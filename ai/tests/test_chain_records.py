"""시연용 사례 체인 기록(data/chain_records/*.json)이 합성 로그와 맞는지 — DB 없이 파일만 본다."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _logs():
    out = {}
    for f in (ROOT / "data" / "logs").glob("*_v1.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        items = d["items"] if isinstance(d, dict) else d
        out[f.stem] = {it["ref"]: it for it in items}
    return out


def _parents(it):
    return ([it["parent_ref"]] if it.get("parent_ref") else []) + list(it.get("parent_refs") or [])


def test_chain_records_point_at_real_roots_fixes_and_edges():
    logs = _logs()
    files = sorted((ROOT / "data" / "chain_records").glob("*.json"))
    assert files
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        for r in doc["resolutions"]:
            items = logs[r["log"]]
            root = items[r["root"]]
            assert root["type"] == "fail" and not _parents(root), f"{f.name}: {r['root']} 은 장애 건(fail 루트)이어야 한다"
            assert r["resolved_by"] and r.get("why"), f"{f.name}: {r['root']} 해결 기록과 이유가 있어야 한다"
            for x in r["resolved_by"]:
                n, seen = items[x], set()
                assert n["type"] == "success", f"{f.name}: {x} 은 해결(success) 기록이어야 한다"
                frontier = _parents(n)   # 위로 올라가 루트에 닿아야 한다(자손)
                while frontier and r["root"] not in frontier:
                    seen.update(frontier)
                    frontier = [p for q in frontier for p in _parents(items[q]) if p not in seen]
                assert r["root"] in frontier, f"{f.name}: {x} 은 {r['root']} 의 자손이 아니다"
        for e in doc["edge_reasons"]:
            child = logs[e["log"]][e["child"]]
            assert e["parent"] in _parents(child), f"{f.name}: 간선 {e['parent']} → {e['child']} 이 로그에 없다"
            assert 0 < len(e["reason"]) <= 300
