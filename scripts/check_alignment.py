"""규정 ↔ 사례(업무 로그) 대조표 점검 — data/alignment/<tenant>.json.

대조표는 "어느 로그 노드가 어느 조문과 어떤 관계인가" 의 단일 원천이다. 두 가지에 쓴다.
  1. 숫자 정합: 같은 값(롤백 기준 1%·재시도 2회 …)이 조문과 로그 본문 양쪽에 실제로 있는지 검사한다. 한쪽만 고치면 여기서 걸린다.
  2. 근거 조문: 적재할 때 로그 노드의 attributes.policy_refs 로 실린다(load_logs.py). 답변이 사례를 인용할 때 그 조문을 함께 근거로 댄다.

관계(relation)
  follows    조문이 적용되는 사례 — 조문대로 처리했거나, 조문이 정한 기준을 지키지 않아 생긴 문제
  exception  조문의 기준과 다르게 처리한 사례 (사유를 note 에 적는다)
  gap        해당 조문이 없는 사례 — 규정 보강 후보 (policy 없이 topic 만)

형식
  {"tenant": "retail", "links": [
     {"log": "sw-dev_v1", "ref": "c6-deploy", "relation": "exception",
      "policy": "retail-deploy-incident-2026-11", "article": "제5조", "values": ["5분", "1%"], "note": "…"},
     {"log": "sw-dev_v1", "ref": "c2-max", "relation": "gap", "topic": "인스턴스별 커넥션 풀 합산 상한", "note": "…"}]}
  values: 조문과 로그 본문(제목·본문·요약) 양쪽에 글자 그대로(공백·쉼표 무시) 있어야 하는 값. policy_values / log_values 로 한쪽만 지정할 수도 있다.

실행: uv run scripts/check_alignment.py [tenant ...] [--table]   (--table: 사람이 읽는 Markdown 표 출력)
종료 코드: 어긋난 항목이 있으면 1.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_policy_pack as vpp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ALIGN = ROOT / "data" / "alignment"
LOGS = ROOT / "data" / "logs"
RELATIONS = ("follows", "exception", "gap")


def load_logs(name: str) -> dict[str, dict]:
    d = json.loads((LOGS / f"{name}.json").read_text(encoding="utf-8"))
    return {i["ref"]: i | {"_ws": d["workspace_id"]} for i in d["items"]}


def article_text(slug: str, article: str, cache: dict) -> str | None:
    """'제5조' / '별표 3' / 'Q7' → 그 조문 글. 문서나 조문이 없으면 None."""
    if slug not in cache:
        md = vpp.SRC / slug / f"{slug}.md"
        cache[slug] = vpp.article_index(md)[1] if md.exists() else None
    idx = cache[slug]
    return None if idx is None else idx.get(re.sub(r"\s+", "", article))


def check(tenant: str) -> tuple[list[str], dict]:
    path = ALIGN / f"{tenant}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    seeds = vpp.seed_index()
    tenant_ws = {w["id"] for w in seeds["ws"].values() if seeds["tenants"].get(w["tenant_id"]) == tenant}
    errs: list[str] = []; logs: dict[str, dict] = {}; arts: dict = {}; seen = Counter()
    for n, l in enumerate(data["links"], 1):
        tag = f"{tenant} #{n} {l.get('log')}:{l.get('ref')}"
        if l.get("relation") not in RELATIONS:
            errs.append(f"{tag}: relation 은 {RELATIONS} 중 하나"); continue
        if l["log"] not in logs:
            if not (LOGS / f"{l['log']}.json").exists():
                errs.append(f"{tag}: data/logs/{l['log']}.json 없음"); continue
            logs[l["log"]] = load_logs(l["log"])
        node = logs[l["log"]].get(l["ref"])
        if node is None:
            errs.append(f"{tag}: 로그에 ref 없음"); continue
        if node["_ws"] not in tenant_ws:
            errs.append(f"{tag}: 로그의 부서 {node['_ws']} 가 회사 '{tenant}' 소속이 아님")
        seen[(l["log"], l["ref"], l.get("policy"), l.get("article"))] += 1
        body = vpp.squash(" ".join(str(node.get(k) or "") for k in ("title", "body", "resolution_summary")))
        if l["relation"] == "gap":
            if not l.get("topic"): errs.append(f"{tag}: gap 은 topic 필요")
            if l.get("policy"): errs.append(f"{tag}: gap 에는 policy 를 적지 않는다")
            continue
        meta_path = vpp.SRC / str(l.get("policy")) / "meta.json"
        if not meta_path.exists():
            errs.append(f"{tag}: 규정 '{l.get('policy')}' 없음"); continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        scope = set()   # 규정 검색 범위 = 로그의 부서 + 조상
        w = node["_ws"]
        while w is not None:
            scope.add(w); w = seeds["ws"][w]["parent_id"] if w in seeds["ws"] else None
        if meta["workspace_id"] not in scope:
            errs.append(f"{tag}: 규정 부서 {meta['workspace_id']} 는 로그 부서 {node['_ws']} 에서 검색되지 않는다(자기 부서·조상만)")
        text = article_text(l["policy"], l.get("article", ""), arts)
        if text is None:
            errs.append(f"{tag}: {l['policy']} 에 {l.get('article')} 없음"); continue
        ptext = vpp.squash(text)
        for v in l.get("values", []) + l.get("policy_values", []):
            if vpp.squash(v) not in ptext: errs.append(f"{tag}: 값 '{v}' 가 {l['article']} 에 없음")
        for v in l.get("values", []) + l.get("log_values", []):
            if vpp.squash(v) not in body: errs.append(f"{tag}: 값 '{v}' 가 로그 본문에 없음")
        if l["relation"] == "exception" and not l.get("note"): errs.append(f"{tag}: exception 은 사유(note) 필요")
    for k, c in seen.items():
        if c > 1: errs.append(f"{tenant}: 같은 연결이 {c}번 — {k}")
    stats = {"links": len(data["links"]), "by": Counter(l["relation"] for l in data["links"]),
             "nodes": len({(l["log"], l["ref"]) for l in data["links"]}), "total_nodes": sum(len(v) for v in logs.values())}
    return errs, stats


def table(tenant: str) -> str:
    data = json.loads((ALIGN / f"{tenant}.json").read_text(encoding="utf-8"))
    rows = ["| 로그 | 노드 | 관계 | 규정 조문 | 맞물리는 값 | 비고 |", "|---|---|---|---|---|---|"]
    for l in data["links"]:
        where = f"{l['policy']} {l['article']}" if l.get("policy") else f"(없음) {l.get('topic', '')}"
        vals = " · ".join(l.get("values", []) + l.get("policy_values", []))
        rows.append(f"| {l['log']} | {l['ref']} | {l['relation']} | {where} | {vals} | {l.get('note', '')} |")
    return "\n".join(rows)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tenants", nargs="*")
    ap.add_argument("--table", action="store_true")
    a = ap.parse_args()
    tenants = a.tenants or sorted(p.stem for p in ALIGN.glob("*.json"))
    bad = 0
    for t in tenants:
        if a.table:
            print(f"## {t}\n\n{table(t)}\n"); continue
        errs, s = check(t)
        bad += bool(errs)
        print(f"{'FAIL' if errs else 'ok  '} {t}: 연결 {s['links']} (follows {s['by']['follows']} · exception {s['by']['exception']} · gap {s['by']['gap']}), "
              f"연결된 노드 {s['nodes']}/{s['total_nodes']}")
        for e in errs: print(f"     {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
