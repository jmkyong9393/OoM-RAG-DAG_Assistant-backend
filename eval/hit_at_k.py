"""검색 품질 평가 hit@k — 어휘(tsvector) vs 벡터(pgvector) vs 하이브리드(RRF). LLM 호출 없음(임베딩만, 있으면).

평가셋: data/evalset/*.json  [{"workspace_id", "query", "expect": ["c:<chunk_id>" | "n:<node_id>", ...], "kind": "policy"|"troubleshoot",
                              "expect_also"?: [...]}]
hit@k = expect 중 하나라도 상위 k 에 있으면 1. 벡터 열은 임베딩이 있을 때만(키 없으면 '-').
expect_also 는 두 문서에 걸친 질의의 둘째 문서 정답 — 둘 다 상위 k 에 있는 수를 따로 낸다(hit@k 에는 넣지 않는다).

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../eval/hit_at_k.py [--k 5] [--json 결과경로] [--record]
      --record: 결과를 eval_runs 에 남긴다(부서별 + 전체 1행, 대시보드의 '검색 품질'). k=5 · 전체 평가셋일 때만. DATABASE_URL(관리자 접속) 필요
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))

from app.config import ai_mode, profile  # noqa: E402
from app.retrieval import db, embed  # noqa: E402
from app.retrieval.tokenize import Tokenizer, extract_error_code, synonym_groups  # noqa: E402


def search(conn, item: dict, mode: str, tokens: str, vec, k: int) -> list[str]:
    ws, kind = item["workspace_id"], item.get("kind", "policy")
    t = tokens if mode in ("lex", "hybrid") else None
    v = vec if mode in ("vec", "hybrid") else None
    if kind == "policy":
        return [f"c:{r['id']}" for r in db.search_chunks(conn, ws, t, v, k)]
    code = extract_error_code(item["query"]) if mode != "vec" else None
    return [f"n:{r['node_id']}" for r in db.search_case_entry(conn, ws, t, v, code, k)]   # 파이프라인이 쓰는 진입점 검색


def summarize(rows: list[dict], mode: str, k: int) -> dict:
    """한 묶음(부서 또는 전체)의 기록용 요약. mrr 은 상위 k 안의 순위만 본다(밖이면 0)."""
    n = len(rows)
    by_kind = {}
    for kind in ("policy", "troubleshoot"):
        rs = [r for r in rows if r["kind"] == kind]
        if rs:
            by_kind[kind] = {"hit": sum(r[mode]["hit"] for r in rs), "n": len(rs)}
    ms = [r["search_ms"] for r in rows if r.get("search_ms") is not None]
    return {"hit_at_k": round(sum(r[mode]["hit"] for r in rows) / n, 4), "n": n,
            "meta": {"k": k, "mrr": round(sum(1 / r[mode]["rank"] if r[mode]["rank"] else 0 for r in rows) / n, 4), "by_kind": by_kind,
                     "search_ms_p50": round(statistics.median(ms), 1) if ms else None}}


def record(rows: list[dict], mode: str, k: int, model: str | None, version: str) -> int:
    """eval_runs 에 부서별 행과 전체(workspace_id NULL) 행을 넣는다. 넣은 행 수를 돌려준다."""
    import psycopg
    from psycopg.types.json import Jsonb
    groups: dict[int | None, list[dict]] = {None: rows}
    for r in rows:
        groups.setdefault(r["workspace_id"], []).append(r)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for ws, rs in groups.items():
            s = summarize(rs, mode, k)
            conn.execute("INSERT INTO eval_runs (workspace_id, dataset_version, mode, is_sample, hit_at_5, n_samples, meta) VALUES (%s, %s, %s, false, %s, %s, %s)",
                         (ws, version, mode, s["hit_at_k"], s["n"], Jsonb({**s["meta"], "embedding_model": model})))
    return len(groups)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 '—' 출력 오류 방지
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("files", nargs="*", type=Path)
    a = ap.parse_args()
    files = a.files or sorted((ROOT / "data" / "evalset").glob("*.json"))
    items = [it for f in files for it in json.loads(f.read_text(encoding="utf-8"))]
    if not items:
        print("평가셋이 없습니다 (data/evalset/*.json)"); return 1
    prof = profile()
    use_vec = ai_mode() == "openai" and prof.has_key
    toks: dict[int, Tokenizer] = {}   # 워크스페이스별 사용자 사전 — 파이프라인과 같은 토크나이저
    modes = ["lex", "vec", "hybrid"] if use_vec else ["lex"]
    rows, hits = [], {m: {"policy": [0, 0], "troubleshoot": [0, 0]} for m in modes}
    syn: dict[int, list[list[str]]] = {}
    with db.connect() as conn:
        for it in items:
            ws = it["workspace_id"]
            groups = syn.get(ws) or syn.setdefault(ws, synonym_groups(db.workspace_settings(conn, ws)["settings"]))
            tok = toks.get(ws) or toks.setdefault(ws, Tokenizer(db.user_terms(conn)))
            tokens = tok.query_tokens(it["query"], groups)   # 파이프라인과 같은 질의 토큰(사용자 사전 + 동의어 확장)
            vec = embed.embed_one(prof, it["query"]) if use_vec else None
            absent = it.get("expect_absent") or []   # 폐기 노드 등 결과에 없어야 하는 id
            also = it.get("expect_also") or []    # 두 문서에 걸친 질의의 둘째 문서 정답
            row = {"query": it["query"], "workspace_id": ws, "kind": it.get("kind", "policy"), "expect": it["expect"], "expect_absent": absent}
            for m in modes:
                t0 = time.perf_counter()
                got = search(conn, it, m, tokens, vec, a.k)
                if m == modes[-1]:
                    row["search_ms"] = (time.perf_counter() - t0) * 1000   # DB 검색만(임베딩 제외)
                hit = (any(e in got for e in it["expect"]) if it["expect"] else True) and not any(x in got for x in absent)
                rank = next((i + 1 for i, g in enumerate(got) if g in it["expect"]), None)
                row[m] = {"hit": hit, "rank": rank, "top": got[:3]}
                if also:
                    row[m]["both"] = hit and any(e in got for e in also)
                hits[m][row["kind"]][0] += int(hit); hits[m][row["kind"]][1] += 1
            rows.append(row)
    print(f"hit@{a.k}  (n={len(items)}, 벡터={'on' if use_vec else 'off — 키 없음'})")
    print(f"{'mode':<8}{'policy':>10}{'troubleshoot':>14}{'all':>8}")
    for m in modes:
        p, t = hits[m]["policy"], hits[m]["troubleshoot"]
        tot = [p[0] + t[0], p[1] + t[1]]
        f = lambda x: f"{x[0]}/{x[1]}" if x[1] else "-"
        print(f"{m:<8}{f(p):>10}{f(t):>14}{f(tot):>8}")
    cross = [r for r in rows if "both" in r[modes[0]]]
    if cross:
        print(f"두 문서에 걸친 질의 {len(cross)}건 — 두 문서의 정답이 모두 상위 {a.k}: " + " · ".join(f"{m} {sum(r[m]['both'] for r in cross)}" for m in modes))
    best = modes[-1]
    total = summarize(rows, best, a.k)
    print(f"{best} MRR@{a.k} {total['meta']['mrr']} · DB 검색 p50 {total['meta']['search_ms_p50']} ms")
    if a.record:
        if a.k != 5 or a.files:
            print("--record 는 k=5 · 전체 평가셋일 때만 쓴다 (eval_runs.hit_at_5)"); return 1
        n = record(rows, best, a.k, prof.embedding_model if use_vec else None, "evalset v1")
        print(f"eval_runs 에 {n}행 기록 (부서별 + 전체)")
    misses = [r for r in rows if not all(r[m]["hit"] for m in modes)]
    if misses:
        print("\n놓친 질의:")
        for r in misses:
            absent = f" absent={r['expect_absent']}" if r["expect_absent"] else ""
            print(f"  [{r['kind']}] {r['query']!r} expect={r['expect']}{absent} " + " ".join(f"{m}:{r[m]['top']}" for m in modes))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"k": a.k, "vector": use_vec, "summary": hits, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
