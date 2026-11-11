"""답변 점검 — 실제 파이프라인을 돌려, 답변이 인용해야 할 근거와 담아야 할 말을 담았는지 본다 (LLM 호출 있음).

점검표: data/answer_checks/*.json  (검색 평가셋 data/evalset 과 형식이 달라 따로 둔다)
  {"workspace_id", "query", "note"?,
   "cite_chunks_any": ["<chunk_id>", …],          # 이 중 하나를 [P…] 로 인용해야 한다
   "cite_nodes_any": [{"log": "sw-dev_v1", "ref": "c5-heap-fix"}, …],   # 이 중 하나를 [N…] 로 인용해야 한다 (id 는 data/logs/<log>.ids.json)
   "contains_all": ["75", "70"], "contains_any": ["기준이 없", "정해져 있지 않"],   # 답변 문장에 들어 있어야 하는 말
   "contains_none": ["9영업일"],
   "cited": true}                                                    # 들어 있으면 안 되는 말(질문과 무관한 사례가 섞였는지)
항목마다 걸린 조건을 전부 만족하면 통과. 조건별 통과 수도 함께 낸다.

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../eval/answer_checks.py [점검표…] [--json 결과경로] [--runs 1]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))

from app.graph import pipeline  # noqa: E402

CHECKS = ("cited", "cite_chunks_any", "cite_nodes_any", "contains_all", "contains_any", "contains_none")


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


def node_ids(refs: list[dict], cache: dict) -> set[int]:
    ids = set()
    for r in refs:
        if r["log"] not in cache:
            cache[r["log"]] = json.loads((ROOT / "data" / "logs" / f"{r['log']}.ids.json").read_text(encoding="utf-8"))["ref"]
        if r["ref"] in cache[r["log"]]:
            ids.add(cache[r["log"]][r["ref"]])
    return ids


async def run_one(item: dict, cache: dict) -> dict:
    buf = b""
    async for chunk in pipeline.run(item["workspace_id"], None, item["query"]):
        buf += chunk
    blocks = parse(buf)
    d = dict(blocks)
    answer = "".join(b[1]["text"] for b in blocks if b[0] == "token")
    cited = set((d.get("citations") or {}).get("verified", []))
    chunks = {p["chunk_id"] for p in d.get("policy") or [] if p["ref"] in cited}
    nodes = {n["id"] for n in (d.get("chain") or {}).get("nodes", []) if n["ref"] in cited}
    res = {}
    if item.get("cited"):                      # 검증된 인용이 하나라도 있다(근거 없이 끝나지 않았다)
        res["cited"] = bool(cited)
    if "cite_chunks_any" in item:
        res["cite_chunks_any"] = bool(chunks & set(item["cite_chunks_any"]))
    if "cite_nodes_any" in item:
        res["cite_nodes_any"] = bool(nodes & node_ids(item["cite_nodes_any"], cache))
    if "contains_all" in item:
        res["contains_all"] = all(s in answer for s in item["contains_all"])
    if "contains_any" in item:
        res["contains_any"] = any(s in answer for s in item["contains_any"])
    if "contains_none" in item:
        res["contains_none"] = not any(s in answer for s in item["contains_none"])
    return {"query": item["query"], "workspace_id": item["workspace_id"], "intent": (d.get("intent") or {}).get("intent"), "checks": res,
            "passed": all(res.values()), "cited": sorted(cited), "answer": answer}


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--runs", type=int, default=1, help="항목마다 반복 횟수 (답변은 실행마다 조금씩 달라진다)")
    ap.add_argument("--allow-stale-schema", action="store_true", help="DB 스키마가 레포보다 옛 판이어도 돌린다")
    a = ap.parse_args()
    import schema_check
    schema_check.ensure_current(a.allow_stale_schema)
    files = a.files or sorted((ROOT / "data" / "answer_checks").glob("*.json"))
    items = [it for f in files for it in json.loads(f.read_text(encoding="utf-8"))]
    cache: dict = {}
    rows, by = [], Counter()
    for it in items:
        for _ in range(a.runs):
            r = await run_one(it, cache)
            rows.append(r)
            by["passed"] += r["passed"]
            by["n"] += 1
            for k, v in r["checks"].items():
                by[k] += v
                by[k + ".n"] += 1
            mark = "ok  " if r["passed"] else "FAIL"
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"{mark} ws{r['workspace_id']} {r['query']}" + (f"  ← {failed}" if failed else ""))
    print(f"\n통과 {by['passed']} / {by['n']}   " + "  ".join(f"{k} {by[k]}/{by[k + '.n']}" for k in CHECKS if by[k + ".n"]))
    if a.json:
        a.json.write_text(json.dumps({"summary": dict(by), "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
