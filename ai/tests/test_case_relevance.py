"""사례 관련성 하한과 규정 폴백. 하한 값은 실측으로 보정한 것이라, 여기서는 규칙의 모양만 고정한다."""
import pytest

from app.graph import pipeline
from tests.conftest import collect, requires_db

FLOOR = pipeline.CaseFloor(near=0.62, far=0.70, terms=2, sure=0.45)


async def _no_case(*a, **k):
    return [], None


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
    assert {"text-embedding-3-small", "text-embedding-3-large"} <= set(pipeline.CASE_FLOOR) and pipeline.CASE_FLOOR.get("unknown-model") is None
    for f in pipeline.CASE_FLOOR.values():
        assert f.sure < f.near < f.far, "판정 생략 < 후보 < 단어 일치가 있을 때의 후보"


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
    monkeypatch.setattr(pipeline, "_graph_search", _no_case)
    blocks = await collect(pipeline.run(2, True, "VPN 접속 권한 문제 예전에 어떻게 풀었어?"))
    d, names = dict(blocks), [b[0] for b in blocks]
    assert d["intent"]["intent"] == "troubleshoot" and "chain" not in names
    stages = [(b[1]["node"], b[1]["status"], b[1].get("detail")) for b in blocks if b[0] == "stage"]
    assert ("graph_search", "done", "no_relevant_case") in stages and ("policy_rag", "start", "fallback") in stages
    assert d["policy"] and all(i.startswith("c:") for i in d["done"]["retrieved_ids"])
    assert names.index("policy") < names.index("token")


@requires_db
async def test_no_fallback_when_policy_is_off(fake_mode, backfilled, monkeypatch):
    monkeypatch.setattr(pipeline, "_graph_search", _no_case)
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


async def test_borderline_candidate_goes_to_judge(monkeypatch):
    """확실한 후보는 판정 없이 채택, 경계선 후보는 후보별 판정을 따르고, 1위 판정이 실패하면 순위대로."""
    calls = []

    def judge(verdicts):
        async def fn(prof, query, case, usage):
            calls.append(case["node_id"])
            v = verdicts[case["node_id"]]
            if isinstance(v, Exception):
                raise v
            return v
        return fn

    sure = {"node_id": 1, "exact": False, "distance": 0.30, "lex_terms": 0}
    a = {"node_id": 2, "exact": False, "distance": 0.55, "lex_terms": 1}
    b = {"node_id": 3, "exact": False, "distance": 0.60, "lex_terms": 2}
    monkeypatch.setattr(pipeline.llm, "judge_case", judge({2: False, 3: True}))
    assert await pipeline._choose_entry(None, "q", [sure, a], FLOOR, None) == 1 and not calls
    assert await pipeline._choose_entry(None, "q", [a, b], FLOOR, None) == 3 and sorted(calls) == [2, 3], "통과한 두 번째 후보"
    monkeypatch.setattr(pipeline.llm, "judge_case", judge({2: False, 3: False}))
    assert await pipeline._choose_entry(None, "q", [a, b], FLOOR, None) is None, "같은 문제가 아니면 사례 없음"
    monkeypatch.setattr(pipeline.llm, "judge_case", judge({2: RuntimeError("llm down"), 3: False}))
    assert await pipeline._choose_entry(None, "q", [a, b], FLOOR, None) == 2, "1위 판정 실패는 순위대로"
    monkeypatch.setattr(pipeline.llm, "judge_case", judge({2: False, 3: RuntimeError("llm down")}))
    assert await pipeline._choose_entry(None, "q", [a, b], FLOOR, None) is None, "뒤 후보의 판정 실패는 통과로 치지 않는다"
    assert await pipeline._choose_entry(None, "q", [], FLOOR, None) is None
    assert await pipeline._choose_entry(None, "q", [a], None, None) == 2, "보정하지 않은 모델은 판정하지 않는다"


def test_close_runner_up_from_another_chain_unsettles_the_top():
    top = {"node_id": 1, "root_id": 1, "exact": False, "distance": 0.39, "lex_terms": 2}
    rival = {"node_id": 5, "root_id": 4, "exact": False, "distance": 0.41, "lex_terms": 2}
    assert pipeline._settled([top], FLOOR) is True
    assert pipeline._settled([top, rival], FLOOR) is False, "다른 체인이 근소하게 뒤따르면 판정에 맡긴다"
    assert pipeline._settled([top, {**rival, "root_id": 1}], FLOOR) is True, "같은 체인의 다른 기록은 경쟁자가 아니다"
    assert pipeline._settled([top, {**rival, "distance": 0.39 + pipeline.SURE_MARGIN + 0.01}], FLOOR) is True
    assert pipeline._settled([{**top, "exact": True}, rival], FLOOR) is True, "에러 코드 일치는 그대로 채택"
    assert pipeline._settled([{**top, "distance": 0.50}], FLOOR) is False
    assert pipeline._settled([top, rival], None) is True, "보정하지 않은 모델은 판정하지 않는다"


async def test_passed_candidates_across_chains_get_compared(monkeypatch):
    """판정을 통과한 후보가 여러 체인이면 가장 가까운 것을 고르게 하고, 한 체인이면 순위대로. 고르기에 실패해도 순위대로."""
    picks = []

    async def yes(prof, query, case, usage):
        return True

    def picker(result):
        async def fn(prof, query, cases, usage):
            picks.append([c["node_id"] for c in cases])
            if isinstance(result, Exception):
                raise result
            return result
        return fn

    a = {"node_id": 2, "root_id": 2, "exact": False, "distance": 0.55, "lex_terms": 2}
    b = {"node_id": 3, "root_id": 2, "exact": False, "distance": 0.58, "lex_terms": 1}
    c = {"node_id": 7, "root_id": 6, "exact": False, "distance": 0.60, "lex_terms": 1}
    monkeypatch.setattr(pipeline.llm, "judge_case", yes)
    monkeypatch.setattr(pipeline.llm, "pick_case", picker(2))
    assert await pipeline._choose_entry(None, "q", [a, b], FLOOR, None) == 2 and not picks, "한 체인이면 고르지 않는다"
    assert await pipeline._choose_entry(None, "q", [a, b, c], FLOOR, None) == 7 and picks == [[2, 3, 7]]
    monkeypatch.setattr(pipeline.llm, "pick_case", picker(RuntimeError("llm down")))
    assert await pipeline._choose_entry(None, "q", [a, b, c], FLOOR, None) == 2


async def test_far_candidates_are_judged_never_trusted(monkeypatch):
    """가까운 후보가 없을 때 올린 후보(weak)는 판정을 통과해야만 쓴다 — 판정이 실패하면 사례 없음."""
    weak = {"node_id": 4, "root_id": 4, "exact": False, "distance": 0.76, "lex_terms": 1, "weak": True}
    assert pipeline._sure({**weak, "distance": 0.30}, FLOOR) is False

    async def verdict(prof, query, case, usage):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(pipeline.llm, "judge_case", verdict)
    answer = True
    assert await pipeline._choose_entry(None, "q", [weak], FLOOR, None) == 4
    answer = False
    assert await pipeline._choose_entry(None, "q", [weak], FLOOR, None) is None
    answer = RuntimeError("llm down")
    assert await pipeline._choose_entry(None, "q", [weak], FLOOR, None) is None


def test_candidates_widen_only_when_they_can_be_judged(monkeypatch):
    """가까운 후보가 없으면 상위 몇 건을 weak 로 올린다. 벡터가 없거나 보정하지 않은 모델이면(판정 불가) 올리지 않는다."""
    from contextlib import contextmanager

    far = [{"node_id": i, "score": 1.0 / i, "exact": False, "lex_terms": 1, "distance": 0.70 + i / 100} for i in range(1, 6)]
    asked = []

    @contextmanager
    def no_conn():
        yield None

    def search(conn, ws, tokens, vec, code, k):
        asked.append(k)
        return [dict(h) for h in far] if vec else [{**h, "distance": None} for h in far]

    monkeypatch.setattr(pipeline.db, "connect", no_conn)
    monkeypatch.setattr(pipeline.db, "search_case_entry", search)
    monkeypatch.setattr(pipeline.db, "case_briefs", lambda conn, ws, ids: {i: {"title": "t", "summary": "s", "root_id": i, "root_title": "r"} for i in ids})
    hits = pipeline._case_candidates(1, "q", [0.1], None, FLOOR)
    assert asked == [pipeline.CASE_POOL] and [h["node_id"] for h in hits] == [1, 2, 3][:pipeline.WEAK_TOP]
    assert all(h["weak"] and h["root_title"] == "r" for h in hits)
    near = pipeline._case_candidates(1, "q", [0.1], None, pipeline.CaseFloor(near=0.74, far=0.74, terms=9, sure=0.1))
    assert [h["node_id"] for h in near] == [1, 2, 3, 4] and not any(h.get("weak") for h in near), "가까운 후보가 있으면 그것만"
    assert not any(h.get("weak") for h in pipeline._case_candidates(1, "q", None, None, FLOOR)), "벡터가 없으면 넓히지 않는다"
    assert not any(h.get("weak") for h in pipeline._case_candidates(1, "q", [0.1], None, None))


async def test_judge_is_skipped_without_key(fake_mode):
    from app.config import profile
    from app.llm import client as llm
    assert await llm.judge_case(profile(), "q", {"root_title": "r", "title": "t", "summary": "s"}, llm.Usage()) is True
    assert await llm.pick_case(profile(), "q", [{"root_title": "r", "title": "t"}, {"root_title": "r2", "title": "t2"}], llm.Usage()) == 0


@requires_db
async def test_case_switch_off_makes_department_policy_only(fake_mode, backfilled, admin_conn):
    """부서 설정에서 사례 사용을 끄면(규정 참조형) 사례 질의에도 체인을 찾지 않고 규정으로 답한다."""
    q = "VPN 접속할 때 E-403 오류가 반복되는데 예전에 어떻게 해결했어?"
    blocks = await collect(pipeline.run(2, None, q))
    assert "chain" in [b[0] for b in blocks]
    admin_conn.execute("UPDATE workspaces SET case_enabled = false WHERE id = 2")
    try:
        blocks = await collect(pipeline.run(2, None, q))
        d, names = dict(blocks), [b[0] for b in blocks]
        assert d["intent"] == {"intent": "policy", "mode": "policy"} and "chain" not in names and "policy" in names
        assert not any(b[0] == "stage" and b[1]["node"] == "graph_search" for b in blocks)
    finally:
        admin_conn.execute("UPDATE workspaces SET case_enabled = true WHERE id = 2")
