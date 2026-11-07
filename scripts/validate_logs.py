"""data/logs/*.json 합성 로그 점검 — 작업 지시(2026-10-29) D-1 완료 기준을 센다.

항목별 오류(규칙 위반)는 ERROR, 완료 기준 미달은 FAIL 로 보고하고 하나라도 있으면 exit 1. 완료 기준은 세트(SETS)별로 센다.
사용: python scripts/validate_logs.py [data/logs/*.json]   (생략 시 data/logs/*_v1.json 전부)
"""
import json, re, sys, pathlib, datetime as dt
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
TYPES = {"fail", "success", "ref"}
DATE_LO, DATE_HI = dt.date(2026, 7, 1), dt.date(2026, 10, 25)
# 세트(회사·차수)마다 완료 기준을 센다. full = 작업 지시 D-1 기준(100건 · 50/35/15 · 정제 비움 30 · 고아 ref 2~3 · 다중 부모 3+).
# 고객지원 세트는 규모가 작아 구조 기준만 본다(폐기 2+ · 질의 수 ≥ 노드 수 · 정제 비움 20%+).
SETS = {
    "retail": {"files": ["sw-dev_v1", "hr-ga_v1"], "full": True, "target": {"sw-dev_v1": 60, "hr-ga_v1": 40}},
    "it": {"files": ["it-dev_v1", "it-cs_v1"], "full": True, "target": {"it-dev_v1": 60, "it-cs_v1": 40}},
    "retail-cs": {"files": ["cs_v1"], "full": False},
    "it-hr": {"files": ["it-hr_v1"], "full": False},
}


def check_file(path: pathlib.Path):
    errors, doc = [], json.loads(path.read_text(encoding="utf-8"))
    ws, items = doc["workspace_id"], doc["items"]
    by_ref = {}
    for i, it in enumerate(items):
        tag = f"{path.name}#{i} {it.get('ref')}"
        if it.get("ref") in by_ref: errors.append(f"{tag}: ref 중복")
        by_ref[it.get("ref")] = it
        if it.get("type") not in TYPES: errors.append(f"{tag}: type")
        if not it.get("title") or not it.get("body"): errors.append(f"{tag}: title/body 비어 있음")
        if it.get("source") != "synthetic": errors.append(f"{tag}: source 는 synthetic")
        if not re.fullmatch(r"syn-[a-z]+-\d{3}", it.get("source_ref", "")): errors.append(f"{tag}: source_ref 형식")
        try:
            d = dt.datetime.fromisoformat(it["created_at"]).date()
            if not DATE_LO <= d <= DATE_HI: errors.append(f"{tag}: created_at 범위 밖 {d}")
        except Exception:
            errors.append(f"{tag}: created_at 파싱 실패")
        parents = ([it["parent_ref"]] if it.get("parent_ref") else []) + list(it.get("parent_refs") or [])
        for p in parents:
            if p not in by_ref: errors.append(f"{tag}: parent '{p}' 가 앞에 없음 (bulk 는 앞 항목만 참조)")
            elif it["type"] == "success" and by_ref[p]["type"] == "success": errors.append(f"{tag}: R12 success 의 부모가 success")
        if it["type"] == "success" and not parents and not it.get("problem_title"): errors.append(f"{tag}: R2 success 루트")
        if it.get("category") is not None and (it["category"] != "cause" or it["type"] != "fail" or not parents):
            errors.append(f"{tag}: R14 category 는 'cause' 이고 상위가 있는 fail 만")
        if it["type"] == "success" and parents:
            root = parents[0]
            while by_ref[root].get("parent_ref"): root = by_ref[root]["parent_ref"]
            if dt.datetime.fromisoformat(it["created_at"]) <= dt.datetime.fromisoformat(by_ref[root]["created_at"]):
                errors.append(f"{tag}: success created_at 이 루트({root})보다 앞 (resolved_at 왜곡)")
        if len(it.get("expected_queries") or []) > 3: errors.append(f"{tag}: expected_queries 는 노드당 0~3 ({len(it['expected_queries'])})")
        refined = bool(it.get("keywords")) or bool(it.get("resolution_summary"))
        if it.get("_refine") and refined: errors.append(f"{tag}: _refine 인데 정제 값이 있음")
        if not it.get("_refine") and not refined: errors.append(f"{tag}: 정제 값도 _refine 표식도 없음")

    counts = Counter(it["type"] for it in items)
    roots = [it for it in items if it["type"] == "fail" and not it.get("parent_ref") and not it.get("parent_refs")]
    multi = [it for it in items if len(it.get("parent_refs") or []) >= 2]
    orphan_ref = [it for it in items if it["type"] == "ref" and not it.get("parent_ref") and not it.get("parent_refs")]
    unrefined = [it for it in items if it.get("_refine")]
    deprec = [it for it in items if it.get("_deprecate_reason")]
    eq = sum(len(it.get("expected_queries") or []) for it in items)
    codes = Counter(it.get("error_code") or "(없음)" for it in items if not it.get("_refine"))
    depth = {}
    for it in items:
        ps = ([it["parent_ref"]] if it.get("parent_ref") else []) + list(it.get("parent_refs") or [])
        depth[it["ref"]] = 0 if not ps else 1 + max(depth[p] for p in ps)
    stats = dict(ws=ws, n=len(items), fail=counts["fail"], success=counts["success"], ref=counts["ref"], chains=len(roots),
                 multi=len(multi), orphan_ref=len(orphan_ref), unrefined=len(unrefined), deprecated=len(deprec),
                 dep_success=sum(1 for d in deprec if d["type"] == "success"), expected_queries=eq, max_depth=max(depth.values()),
                 codes=dict(codes))
    return errors, stats


def main():
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔(cp949)에서 '✓' 출력 오류 방지
    files = [pathlib.Path(a) for a in sys.argv[1:]] or sorted((ROOT / "data" / "logs").glob("*_v1.json"))
    all_err, fails, stats = [], [], {}
    for f in files:
        err, st = check_file(f)
        all_err += err
        stats[f.stem] = st
        print(f"{f.name}: ws{st['ws']} n={st['n']} fail/success/ref={st['fail']}/{st['success']}/{st['ref']} chains={st['chains']} "
              f"multi={st['multi']} orphan_ref={st['orphan_ref']} unrefined={st['unrefined']} deprecated={st['deprecated']}"
              f"(success {st['dep_success']}) eq={st['expected_queries']} depth≤{st['max_depth']}")
        print("   error_code:", st["codes"])
        if st["chains"] < 6: all_err.append(f"FAIL {f.name}: 체인 6+ ({st['chains']})")
        if f.stem not in {m for spec in SETS.values() for m in spec["files"]}: fails.append(f"{f.name}: 어느 세트에도 속하지 않음 (SETS 에 추가)")
    for name, spec in SETS.items():
        got = [m for m in spec["files"] if m in stats]
        if not got:
            continue
        if len(got) != len(spec["files"]):   # 세트 일부만 넘기면 합계 기준은 볼 수 없다
            print(f"[{name}] 일부 파일만 점검 — 세트 기준 생략")
            continue
        tot = Counter()
        for m in got:
            for k, v in stats[m].items():
                if isinstance(v, int): tot[k] += v
            want = spec.get("target", {}).get(m)
            if want and stats[m]["n"] != want: fails.append(f"[{name}] {m}: {want}건이어야 함 ({stats[m]['n']})")
        print(f"[{name}] 합계 n={tot['n']} fail/success/ref={tot['fail']}/{tot['success']}/{tot['ref']} multi={tot['multi']} orphan={tot['orphan_ref']} "
              f"unrefined={tot['unrefined']} deprecated={tot['deprecated']} eq={tot['expected_queries']}")
        if spec["full"]:
            if tot["n"] != 100: fails.append(f"[{name}] 총 100건 ({tot['n']})")
            if (tot["fail"], tot["success"], tot["ref"]) != (50, 35, 15): fails.append(f"[{name}] 타입 비율 50/35/15 ({tot['fail']}/{tot['success']}/{tot['ref']})")
            if tot["multi"] < 3: fails.append(f"[{name}] 다중 부모 3+ ({tot['multi']})")
            if not 2 <= tot["orphan_ref"] <= 3: fails.append(f"[{name}] 고아 ref 2~3 ({tot['orphan_ref']})")
            if tot["unrefined"] != 30: fails.append(f"[{name}] 정제 비움 30 ({tot['unrefined']})")
            if tot["deprecated"] < 3 or tot["dep_success"] < 1: fails.append(f"[{name}] 폐기 3+ (success 1+) ({tot['deprecated']}, success {tot['dep_success']})")
            if tot["expected_queries"] < 60: fails.append(f"[{name}] expected_queries 60+ ({tot['expected_queries']})")
        else:
            if tot["deprecated"] < 2: fails.append(f"[{name}] 폐기 2+ ({tot['deprecated']})")
            if tot["expected_queries"] < tot["n"]: fails.append(f"[{name}] expected_queries 가 노드 수 이상 ({tot['expected_queries']} < {tot['n']})")
            if tot["unrefined"] < tot["n"] // 5: fails.append(f"[{name}] 정제 비움 20%+ ({tot['unrefined']})")
    if all_err or fails:
        print(f"\n✗ NG — 실패 원인 {len(all_err) + len(fails)}건")
        for e in all_err: print(f"  [규칙 위반] {e}")
        for f in fails: print(f"  [기준 미달] {f}")
        sys.exit(1)
    print("\n✓ OK — 규칙 위반 0 · 완료 기준 전부 충족")


if __name__ == "__main__":
    main()

