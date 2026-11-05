"""첫 화면 추천 질문(시드 부서 설정)은 전부 답변 점검표에 들어 있어야 한다 — 근거가 붙는지 확인된 질문만 화면에 낸다. DB 불필요."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_seed_suggestion_is_checked():
    seed = {(w["id"], q) for w in json.loads((ROOT / "data" / "seed" / "workspaces.json").read_text(encoding="utf-8"))
            for q in w.get("settings", {}).get("suggestions", [])}
    checked = {(i["workspace_id"], i["query"]) for i in json.loads((ROOT / "data" / "answer_checks" / "suggestions_v1.json").read_text(encoding="utf-8"))}
    assert seed and seed <= checked, f"점검표에 없는 추천 질문: {sorted(seed - checked)}"
    depts = {w["id"] for w in json.loads((ROOT / "data" / "seed" / "workspaces.json").read_text(encoding="utf-8")) if w["kind"] == "dept"}
    assert {ws for ws, _ in seed} == depts, "부서마다 추천 질문이 있어야 한다"
