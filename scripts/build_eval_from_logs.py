"""합성 로그의 expected_queries → 트러블슈팅 평가셋 (D-3).

data/logs/*_v1.json + *.ids.json 에서 질의 30건을 뽑아 data/evalset/synthetic_logs_v1.json 을 쓴다.
  분류(우선순위 순): colloquial(구어·오타, README 표) → code(질의에 그 노드의 에러 코드 포함) → reverse(success 역질의) → symptom(루트 fail 증상)
  할당: code 10 · symptom 10 · reverse 5 · colloquial 5. 각 분류 안에서 체인이 고르게 섞이도록 등간격으로 고른다.
  expect = 질의가 붙은 노드 + 같은 체인의 루트 + 같은 체인의 active success (지시서 D-3).
  폐기 노드 질의 2건은 expect_absent(폐기 노드) + expect(대체 노드) — hit_at_k.py 의 expect_absent 지원 필요.

사용: python scripts/build_eval_from_logs.py        (먼저 make load-logs 로 *.ids.json 을 만든다)
"""
import json, re, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "evalset" / "synthetic_logs_v1.json"
QUOTA = {"code": 10, "symptom": 10, "reverse": 5, "colloquial": 5}
COLLOQUIAL = {  # README "의도적으로 어렵게 쓴 질의" 중 평가에 넣을 5 (lex 로 못 맞히는 4 + 구어 1)
    "커넥션풀 꽉찼을때 어케했어", "빌드 캐쉬 깨졌을때 어떻게 했어", "비번 바꿨는데 계정이 계속 잠겨요",
    "서킷브레이커 적용한 사례", "결재 올리면 ora 에러나요",
}
ABSENT = {  # 폐기 노드 질의 → 폐기 노드는 결과에 없어야 하고, 대체 노드가 나와야 한다
    "c1-manual": ("토큰 수동으로 재발급하는 스크립트 어디 있어?", "c1-cron"),
    "h2-form": ("잠금 해제 요청 양식 어디 있어?", "h2-self"),
}


def norm(s: str) -> str:
    return re.sub(r"[-\s]", "", s).lower()


def spread(cands: list, n: int) -> list:
    if len(cands) <= n:
        return cands
    return [cands[round(i * (len(cands) - 1) / (n - 1))] for i in range(n)] if n > 1 else cands[:1]


def main():
    items, absent_items = [], []
    for f in sorted((ROOT / "data" / "logs").glob("*_v1.json")):
        ids_path = f.with_suffix(".ids.json")
        if not ids_path.exists():
            sys.exit(f"{ids_path.name} 없음 — 먼저 make load-logs")
        doc = json.loads(f.read_text(encoding="utf-8")); ids = json.loads(ids_path.read_text(encoding="utf-8"))["ref"]
        ws, nodes = doc["workspace_id"], doc["items"]
        by = {n["ref"]: n for n in nodes}
        parents = lambda n: ([n["parent_ref"]] if n.get("parent_ref") else []) + list(n.get("parent_refs") or [])

        def root_of(ref):
            while parents(by[ref]): ref = parents(by[ref])[0]
            return ref
        chain_success = {}
        for n in nodes:
            r = root_of(n["ref"])
            if n["type"] == "success" and not n.get("_deprecate_reason"):
                chain_success.setdefault(r, []).append(n["ref"])

        for n in nodes:
            r = root_of(n["ref"]); is_root = r == n["ref"] and n["type"] == "fail"
            for q in n.get("expected_queries", []):
                if n["ref"] in ABSENT and q == ABSENT[n["ref"]][0]:
                    absent_items.append({"workspace_id": ws, "kind": "troubleshoot", "query": q, "expect": [f"n:{ids[ABSENT[n['ref']][1]]}"],
                                         "expect_absent": [f"n:{ids[n['ref']]}"], "note": f"폐기 노드 {n['ref']} 제외, 대체 {ABSENT[n['ref']][1]}"})
                    continue
                code = n.get("error_code") or by[r].get("error_code")
                if q in COLLOQUIAL: cat = "colloquial"
                elif code and (norm(code) in norm(q) or re.search(r"\d{3}", code) and re.search(r"\d{3}", code).group() in q): cat = "code"
                elif n["type"] == "success": cat = "reverse"
                elif is_root: cat = "symptom"
                else: cat = "other"
                expect = {f"n:{ids[n['ref']]}", f"n:{ids[r]}"} | {f"n:{ids[s]}" for s in chain_success.get(r, [])}
                items.append({"cat": cat, "ref": n["ref"], "workspace_id": ws, "kind": "troubleshoot", "query": q,
                              "expect": sorted(expect, key=lambda x: int(x[2:])), "note": f"{cat} · {n['ref']}"})

    picked = []
    for cat, n in QUOTA.items():
        cands = [i for i in items if i["cat"] == cat]
        if len(cands) < n:
            sys.exit(f"{cat} 후보 {len(cands)}개 < 할당 {n}")
        picked += spread(cands, n)
    out = [{k: v for k, v in i.items() if k not in ("cat", "ref")} for i in picked] + absent_items
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {c: sum(1 for i in picked if i["cat"] == c) for c in QUOTA}
    print(f"{len(out)}건 → {OUT.relative_to(ROOT)}  {counts} + expect_absent {len(absent_items)}")
    for i in picked + absent_items:
        print(f"  [{i.get('cat', 'absent'):10}] ws{i['workspace_id']} {i['query']}  → {i['expect']}{' ✗' + str(i['expect_absent']) if i.get('expect_absent') else ''}")


if __name__ == "__main__":
    main()
