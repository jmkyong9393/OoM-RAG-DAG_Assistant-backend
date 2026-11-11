"""검색 평가셋(정답 조문 · 사례가 붙은 질문)으로 실제 답변을 만들어, 채점 모델 없이 셀 수 있는 답변 실패를 센다 (LLM 호출: 재순위 · 판정 · 답변).

답변 점검표(48문항)는 손으로 고른 질문이라, 규정 전체에 걸친 실패 유형과 빈도를 알 수 없다. 평가셋은 질문마다 정답 id 가 있어 답변 단계를 기계로 가를 수 있다.
  cited    정답이 근거에 있고 답변이 그것을 인용했다
  refused  정답이 근거에 있는데 인용하지 않고 첫 문장에서 '기준이 없다'고 답했다   ← 거짓 거절
  uncited  정답이 근거에 있는데 인용하지 않았다(다른 근거로 답함)
  missed   정답이 근거에 없다(검색 · 진입점 선택 단계의 실패)
그 밖에: bare_gap(답변이 '기준 없음' 두 문장 이하 — 관련 조문 안내가 없는 빈 답), 인용 없는 답변, 검증 안 된 마커.

실행: set -a; . deploy/.env; set +a; cd ai && uv run --extra graph python ../eval/answer_on_evalset.py [평가셋…] [--json 결과] [--concurrency 6] [--kind policy|troubleshoot]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))
sys.path.insert(0, str(ROOT / "eval"))

from app.config import ai_mode, profile  # noqa: E402
from app.graph import pipeline  # noqa: E402
from app.llm import client as llm  # noqa: E402

SENT = re.compile(r"[^.!?\n]+[.!?]?")
MARK = re.compile(r"\s*\[[PN]\d+(?:\s*,\s*[PN]\d+)*\]")


def parse(buf: bytes) -> list[tuple[str, object]]:
    out = []
    for raw in buf.decode("utf-8").split("\n\n"):
        ev, data = None, []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if ev:
            out.append((ev, json.loads("\n".join(data))))
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT.findall(MARK.sub("", text)) if len(s.strip()) > 1]


def classify(expect: list[str], retrieved: list[str], verified: list[str], answer: str) -> dict:
    """한 답변의 분류와 표식. expect 가 비면(정답 없음 문항) 분류는 none."""
    body = answer.split("\n\n" + llm.NOTICE_PREFIX)[0]
    sents = sentences(body)
    gap_first = bool(sents and llm.GAP_ADMITTED.search(sents[0]))
    in_ctx = any(e in retrieved for e in expect)
    cited = any(e in verified for e in expect)
    if not expect:
        kind = "none"
    elif not in_ctx:
        kind = "missed"
    elif cited:
        kind = "cited"       # 정답을 인용했으면 답한 것이다 — '규정에는 없으나 사례에서는 …' 처럼 없다는 말로 시작해도
    elif gap_first:
        kind = "refused"
    else:
        kind = "uncited"
    return {"class": kind, "in_context": in_ctx, "cited": cited, "gap_first": gap_first, "gap_any": bool(llm.GAP_ADMITTED.search(body)),
            "gap_then_cited": gap_first and cited,   # 없다고 시작하고 정답을 인용 — 서두가 모순일 수 있어 따로 센다
            "bare_gap": gap_first and len(sents) <= 2, "sentences": len(sents), "no_citation": not verified}


async def run_one(item: dict) -> dict:
    t0 = time.perf_counter()
    buf = b""
    async for chunk in pipeline.run(item["workspace_id"], None, item["query"]):
        buf += chunk
    blocks = parse(buf)
    d = dict(blocks)
    answer = "".join(b[1]["text"] for b in blocks if b[0] == "token")
    done = d.get("done") or {}
    row = {"workspace_id": item["workspace_id"], "query": item["query"], "kind": item.get("kind", "policy"), "expect": item["expect"],
           "retrieved": done.get("retrieved_ids", []), "verified": done.get("verified_ids", []), "unverified": done.get("unverified_ids", []),
           "intent": (d.get("intent") or {}).get("intent"), "answer": answer, "error": (d.get("error") or {}).get("message"),
           "sec": round(time.perf_counter() - t0, 1)}
    row.update(classify(item["expect"], row["retrieved"], row["verified"], answer))
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
    for kind in ("policy", "troubleshoot"):
        rs = [r for r in rows if r["kind"] == kind and r["class"] != "none"]
        if not rs:
            continue
        c = Counter(r["class"] for r in rs)
        in_ctx = len(rs) - c["missed"]
        out[kind] = {"n": len(rs), "in_context": in_ctx, **{k: c[k] for k in ("cited", "refused", "uncited", "missed")},
                     "cited_of_in_context": round(c["cited"] / in_ctx, 4) if in_ctx else None,
                     "bare_gap": sum(r["bare_gap"] for r in rs), "gap_any": sum(r["gap_any"] for r in rs),
                     "gap_then_cited": sum(r["gap_then_cited"] for r in rs),
                     "no_citation": sum(r["no_citation"] for r in rs), "unverified_markers": sum(bool(r["unverified"]) for r in rs),
                     "errors": sum(bool(r["error"]) for r in rs)}
    return out


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--kind", choices=["policy", "troubleshoot"], default=None)
    ap.add_argument("--allow-stale-schema", action="store_true", help="DB 스키마가 레포보다 옛 판이어도 돌린다")
    a = ap.parse_args()
    import schema_check
    schema_check.ensure_current(a.allow_stale_schema)
    if ai_mode() != "openai" or not profile().has_key:
        print("실제 답변이 필요하다 — openai 모드에서만 돈다 (LLM 키 필요)"); return 1
    files = a.files or sorted((ROOT / "data" / "evalset").glob("*.json"))
    items = [it for f in files for it in json.loads(f.read_text(encoding="utf-8")) if not a.kind or it.get("kind", "policy") == a.kind]
    sem = asyncio.Semaphore(a.concurrency)
    done = 0

    async def guarded(it: dict) -> dict:
        nonlocal done
        async with sem:
            try:
                r = await run_one(it)
            except Exception as e:  # noqa: BLE001 — 한 문항 실패로 전체를 멈추지 않는다
                r = {"workspace_id": it["workspace_id"], "query": it["query"], "kind": it.get("kind", "policy"), "expect": it["expect"],
                     "retrieved": [], "verified": [], "unverified": [], "intent": None, "answer": "", "error": f"{type(e).__name__}: {e}", "sec": 0,
                     **classify(it["expect"], [], [], "")}
            done += 1
            if done % 25 == 0:
                print(f"  … {done}/{len(items)}", flush=True)
            return r
    t0 = time.perf_counter()
    rows = await asyncio.gather(*(guarded(it) for it in items))
    s = summarize(rows)
    print(f"답변 {len(rows)}건 · {time.perf_counter() - t0:.0f}초 (동시 {a.concurrency})")
    for kind, v in s.items():
        print(f"[{kind}] 정답이 근거에 {v['in_context']}/{v['n']} — 인용 {v['cited']} · 거짓 거절 {v['refused']} · 인용 안 함 {v['uncited']} | 놓침 {v['missed']}"
              f" | 빈 답 {v['bare_gap']} · 없다고 시작하고 인용 {v['gap_then_cited']} · 인용 없는 답변 {v['no_citation']} · 검증 안 된 마커 {v['unverified_markers']} · 오류 {v['errors']}")
    for label, key in (("거짓 거절", "refused"), ("인용 안 함", "uncited")):
        for r in [r for r in rows if r["class"] == key][:60]:
            print(f"  [{label}] ws{r['workspace_id']} {r['query']} → {MARK.sub('', r['answer'])[:90]!r}")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"summary": s, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
