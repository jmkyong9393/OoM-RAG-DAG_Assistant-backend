"""사례 관련성 하한과 규정 폴백. 하한 값은 실측으로 보정한 것이라, 여기서는 규칙의 모양만 고정한다."""
import pytest

from app.graph import pipeline
from tests.conftest import collect, requires_db

FLOOR = (0.62, 0.70, 2)


@pytest.mark.parametrize("hit,expected", [
    ({"exact": True, "distance": 0.95, "lex_terms": 0}, True),     # 에러 코드가 일치하면 거리와 무관
    ({"exact": False, "distance": 0.40, "lex_terms": 0}, True),    # 가깝다
    ({"exact": False, "distance": 0.66, "lex_terms": 2}, True),    # 조금 멀어도 질의 단어 2개 일치
    ({"exact": False, "distance": 0.66, "lex_terms": 1}, False),
    ({"exact": False, "distance": 0.75, "lex_terms": 5}, False),   # 멀면 단어가 겹쳐도 근거로 쓰지 않는다
    ({"exact": False, "distance": None, "lex_terms": 0}, True),    # 벡터가 없으면(fake 모드) 판단하지 않는다
])
def test_relevance_floor(hit, expected):
    assert pipeline._relevant(hit, FLOOR) is expected


def test_floor_only_for_calibrated_models():
    assert pipeline._relevant({"exact": False, "distance": 0.99, "lex_terms": 0}, None) is True
    assert "text-embedding-3-small" in pipeline.CASE_FLOOR and pipeline.CASE_FLOOR.get("unknown-model") is None


@requires_db
async def test_entry_search_returns_evidence(fake_mode, backfilled):
    from app.retrieval import db
    from app.retrieval.tokenize import Tokenizer
    with db.connect() as conn:
        hits = db.search_case_entry(conn, 1, Tokenizer().tokens("VPN 접속 E-403 오류"), None, "E-403", 3)
        assert hits and hits[0]["exact"] is True and hits[0]["lex_terms"] >= 1 and hits[0]["distance"] is None
        assert len(hits) <= 3 and hits == sorted(hits, key=lambda h: (-h["score"], h["node_id"]))
        assert not db.search_case_entry(conn, 3, "vpn", None, "E-403", 3), "사례가 없는 부서"


@requires_db
async def test_falls_back_to_policy_when_no_relevant_case(fake_mode, backfilled, monkeypatch):
    """사례 질의로 분류됐지만 관련 사례가 없으면 규정에서 근거를 찾는다."""
    monkeypatch.setattr(pipeline, "_graph_search", lambda *a, **k: ([], None))
    blocks = await collect(pipeline.run(2, True, "VPN 접속 권한 문제 예전에 어떻게 풀었어?"))
    d, names = dict(blocks), [b[0] for b in blocks]
    assert d["intent"]["intent"] == "troubleshoot" and "chain" not in names
    stages = [(b[1]["node"], b[1]["status"], b[1].get("detail")) for b in blocks if b[0] == "stage"]
    assert ("graph_search", "done", "no_relevant_case") in stages and ("policy_rag", "start", "fallback") in stages
    assert d["policy"] and all(i.startswith("c:") for i in d["done"]["retrieved_ids"])
    assert names.index("policy") < names.index("token")


@requires_db
async def test_no_fallback_when_policy_is_off(fake_mode, backfilled, monkeypatch):
    monkeypatch.setattr(pipeline, "_graph_search", lambda *a, **k: ([], None))
    blocks = await collect(pipeline.run(2, False, "VPN 접속 권한 문제 예전에 어떻게 풀었어?"))
    names = [b[0] for b in blocks]
    assert "policy" not in names and dict(blocks)["done"]["model"] == "gate"


def test_company_rules_keep_two_slots_among_external_standards():
    def row(i, t):
        return {"id": i, "doc_type": t}
    ext = [row(f"e{i}", "고시") for i in range(8)]
    rows = ext[:6] + [row("c1", "규정"), ext[6], row("c2", "지침"), row("c3", "FAQ"), ext[7]]
    assert [r["id"] for r in pipeline._with_company_rules(rows)] == ["e0", "e1", "e2", "c1", "c2"], "낮은 순위 고시 2건을 회사 규정으로 바꾼다"
    mixed = [row("c1", "규정"), ext[0], row("c2", "약관"), ext[1], ext[2], row("c3", "규정")]
    assert pipeline._with_company_rules(mixed) == mixed[:5], "이미 2건이면 순위 그대로"
    assert pipeline._with_company_rules(ext) == ext[:5], "회사 규정 후보가 없으면 그대로"
    one = ext[:5] + [row("c1", "규정")]
    assert [r["id"] for r in pipeline._with_company_rules(one)] == ["e0", "e1", "e2", "e3", "c1"]
