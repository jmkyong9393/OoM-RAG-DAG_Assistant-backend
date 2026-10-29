"""검색 품질 평가 hit@k — 어휘(tsvector) vs 벡터(pgvector) vs 하이브리드(RRF). LLM 호출 없음(임베딩만, 있으면).

평가셋: data/evalset/*.json  [{"workspace_id", "query", "expect": ["c:<chunk_id>" | "n:<node_id>", ...], "kind": "policy"|"troubleshoot"}]
hit@k = expect 중 하나라도 상위 k 에 있으면 1. 벡터 열은 임베딩이 있을 때만(키 없으면 '-').

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../eval/hit_at_k.py [--k 5] [--json 결과경로]
"""
from __future__ import annotations

import argparse
import json
import sys
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
    return [f"n:{r['node_id']}" for r in db.search_nodes(conn, ws, t, v, code, k)]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 '—' 출력 오류 방지
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", type=Path, default=None)
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
            tok = toks.get(ws) or toks.setdefault(ws, Tokenizer(db.workspace_terms(conn, ws)))
            tokens = tok.query_tokens(it["query"], groups)   # 파이프라인과 같은 질의 토큰(사용자 사전 + 동의어 확장)
            vec = embed.embed_one(prof, it["query"]) if use_vec else None
            row = {"query": it["query"], "kind": it.get("kind", "policy"), "expect": it["expect"]}
            for m in modes:
                got = search(conn, it, m, tokens, vec, a.k)
                hit = any(e in got for e in it["expect"])
                rank = next((i + 1 for i, g in enumerate(got) if g in it["expect"]), None)
                row[m] = {"hit": hit, "rank": rank, "top": got[:3]}
                hits[m][row["kind"]][0] += int(hit); hits[m][row["kind"]][1] += 1
            rows.append(row)
    print(f"hit@{a.k}  (n={len(items)}, 벡터={'on' if use_vec else 'off — 키 없음'})")
    print(f"{'mode':<8}{'policy':>10}{'troubleshoot':>14}{'all':>8}")
    for m in modes:
        p, t = hits[m]["policy"], hits[m]["troubleshoot"]
        tot = [p[0] + t[0], p[1] + t[1]]
        f = lambda x: f"{x[0]}/{x[1]}" if x[1] else "-"
        print(f"{m:<8}{f(p):>10}{f(t):>14}{f(tot):>8}")
    misses = [r for r in rows if not all(r[m]["hit"] for m in modes)]
    if misses:
        print("\n놓친 질의:")
        for r in misses:
            print(f"  [{r['kind']}] {r['query']!r} expect={r['expect']} " + " ".join(f"{m}:{r[m]['top']}" for m in modes))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"k": a.k, "vector": use_vec, "summary": hits, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
