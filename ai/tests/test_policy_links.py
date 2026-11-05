"""사례와 규정의 연결: 체인이 따른 조문을 근거에 싣고, 조문에 연결된 사례를 후보에 더한다."""
import json

import pytest

from app.graph import pipeline
from app.llm import client as llm
from app.llm.schemas import ChainEvent, ChainNode
from app.retrieval import db
from tests.conftest import collect, requires_db

FLOOR = pipeline.CaseFloor(near=0.62, far=0.70, terms=2, sure=0.45)


def _chunk(cid, slug="ops", label="제9조 (커넥션 풀)"):
    return {"id": cid, "doc_id": 1, "slug": slug, "doc_title": "운영지침", "version": "1", "doc_type": "지침", "article_label": label, "page": None, "text": "본문"}


def _node(i, depth, refs, type_="fail", status="active"):
    return {"ref": f"N{i}", "id": i, "type": type_, "status": status, "title": f"t{i}", "resolution_summary": None, "depth": depth, "policy_refs": refs}


def _event(nodes, entry):
    return ChainEvent(entry_id=entry, edges=[], nodes=[ChainNode(ref=n["ref"], id=n["id"], type=n["type"], status=n["status"], title=n["title"], depth=n["depth"]) for n in nodes])


def test_article_keys_and_label_match():
    ps = [{"slug": "ops", "article_label": "제5조 (배포 후 관찰과 롤백)"}, {"slug": "ops", "article_label": "[별표 3] 서비스 운영 기준"},
          {"slug": "ops", "article_label": "제5조 (배포 후 관찰과 롤백)"}, {"slug": "faq", "article_label": "Q3. 인증번호가 안 와요"}]
    assert pipeline._article_keys(ps) == [("ops", "제5조"), ("ops", "별표 3"), ("faq", "Q3")], "FAQ 항목도 사례와 이어지는 조문이다"
    assert db.same_article("Q3. 인증번호가 안 와요", "Q3") and not db.same_article("Q30. 탈퇴", "Q3")
    assert db.same_article("제1조 (목적)", "제1조") and not db.same_article("제10조 (자원)", "제1조")
    assert db.same_article("[별표 2] 배포 전 점검표", "별표 2") and not db.same_article("[별표 21] x", "별표 2")


def test_link_policies_adds_evidence_and_marks_nodes(monkeypatch):
    chunks = {("ops", "제9조"): _chunk("ops-a9-1"), ("ops", "제5조"): _chunk("ops-a5-1", label="제5조 (롤백)"), ("ops", "제7조"): _chunk("ops-a7-1", label="제7조 (연동)")}
    monkeypatch.setattr(pipeline.db, "connect", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(pipeline.db, "policy_ref_chunks", lambda conn, ws, keys: {k: chunks[k] for k in keys if k in chunks})
    nodes = [_node(1, 0, [{"policy": "ops", "article": "제7조", "relation": "follows"}]),
             _node(2, 1, [{"policy": "ops", "article": "제9조", "relation": "follows", "note": "풀 20"}]),
             _node(3, 1, [{"policy": "ops", "article": "제5조", "relation": "exception", "note": "0.6% 인데 롤백"}], type_="success"),
             _node(4, 1, [{"relation": "gap", "topic": "합산 상한"}]),
             _node(5, 1, [{"policy": "ops", "article": "제99조", "relation": "follows"}]),
             _node(6, 1, [{"policy": "ops", "article": "제9조", "relation": "follows"}], status="deprecated")]
    # 규정 검색 결과가 이미 있을 때: 진입(2)·루트(1)·예외(3)의 조문만 새로 싣는다. 이미 실린 조문은 번호를 다시 쓰지 않는다
    policy = [pipeline._policy_entry(1, _chunk("ops-a9-1"), "search")]
    ev = _event(nodes, entry=2)
    assert pipeline._link_policies(1, nodes, ev, policy) == 2
    assert [(p["ref"], p["chunk_id"], p["source"]) for p in policy] == [("P1", "ops-a9-1", "search"), ("P2", "ops-a5-1", "case"), ("P3", "ops-a7-1", "case")]
    marks = {n["id"]: n.get("policy") for n in nodes}
    assert marks[2] == [{"relation": "follows", "ref": "P1", "label": "운영지침 제9조", "note": "풀 20"}]
    assert marks[3][0]["relation"] == "exception" and marks[3][0]["ref"] == "P2"
    assert marks[4] == [{"relation": "gap", "topic": "합산 상한", "note": None}]
    assert marks[5] is None and marks[6] is None, "없는 조문·폐기 노드는 표시하지 않는다"
    assert {e.id: [m.ref for m in e.policy] for e in ev.nodes if e.policy} == {1: ["P3"], 2: ["P1"], 3: ["P2"], 4: [None]}
    # 근거 표시는 모델에게 주는 자료로 렌더링된다
    text = llm._render_context(policy, nodes)
    assert "(규정 관계: P2 운영지침 제5조 — 규정과 달리 처리한 예외. 0.6% 인데 롤백)" in text and "규정에 기준이 없다 — 합산 상한" in text


def test_link_policies_marks_cases_whose_rule_was_revised(monkeypatch):
    """개정된 조문: 내용이 바뀐 조문은 지금 조문을 근거로 싣고 표시한다. 대응 조문이 없으면 근거로 싣지 않고 개정 전 조문만 알린다."""
    chunks = {("ops-1", "제10조"): {**_chunk("ops-2-a10-1", label="제10조 (컨테이너 자원)"), "status": "amended", "from_version": "v1"},
              ("ops-1", "제11조"): {"id": None, "chunk_id": None, "status": "replaced", "from_title": "운영지침", "from_version": "v1",
                                   "from_label": "제11조 (로그 보관)", "suggested_label": "제12조 (로그 보관 기간)"}}
    monkeypatch.setattr(pipeline.db, "connect", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(pipeline.db, "policy_ref_chunks", lambda conn, ws, keys: {k: chunks[k] for k in keys if k in chunks})
    nodes = [_node(1, 0, [{"policy": "ops-1", "article": "제10조", "relation": "exception", "note": "당시 75%. 70% 가 현재 기준"}], type_="success"),
             _node(2, 1, [{"policy": "ops-1", "article": "제11조", "relation": "follows"}]),
             _node(3, 1, [{"relation": "gap", "topic": "합산 상한"}])]
    for n in nodes:
        n["near"] = True
    nodes[0]["entry"] = True
    policy: list[dict] = []
    assert pipeline._link_policies(1, nodes, _event(nodes, entry=1), policy) == 1, "대응 조문이 없는 근거는 싣지 않는다"
    assert nodes[0]["policy"][0] == {"relation": "exception", "ref": "P1", "label": "운영지침 제10조", "note": "당시 75%. 70% 가 현재 기준",
                                     "revision": "amended", "from_version": "v1"}
    assert nodes[1]["policy"][0]["ref"] is None and nodes[1]["policy"][0]["revision"] == "replaced" and nodes[1]["policy"][0]["label"] == "운영지침 v1 제11조"
    assert nodes[2]["policy"][0]["revision"] == "replaced", "'기준 없음'도 개정 전의 판단으로 표시한다"
    text = llm._render_context(policy, nodes)
    assert "개정 전에 적은 메모 — 당시 75%. 70% 가 당시(v1 판) 기준" in text and "사례는 v1 판 기준" in text
    assert "현재 기준" not in text, "개정된 사례의 메모가 말하는 '현재 기준'은 개정 전 기준으로 바꿔 넘긴다"
    assert "개정 전 규정 운영지침 v1 제11조 기준의 처리" in text and "처리 당시에는 규정에 기준이 없었다" in text
    must = llm._must_include(nodes)
    assert any("개정되기 전(v1 판)" in m and "[P1]" in m for m in must), "지금 조문의 값을 현재 기준으로 쓰게 한다"
    assert any("개정 전 규정(운영지침 v1 제11조)" in m for m in must)
    assert not any("[None]" in m for m in must), "마커가 없는 근거를 마커로 쓰지 않는다"
    assert any("70% 가 당시(v1 판) 기준" in m for m in must) and not any("70% 가 현재 기준" in m for m in must), "예외 사례 지시에도 같은 치환"
    # 개정되지 않은 사례의 메모는 그대로다
    assert llm._note_text({"note": "70% 가 현재 기준"}) == "70% 가 현재 기준"
    # 진입 사례가 '기준 없음'이거나, 따르던 조문이 개정판에 없을 때도 없는 라벨·마커를 지시에 넣지 않는다
    for entry in (nodes[2], nodes[1]):
        for n in nodes:
            n["entry"] = n is entry
        must = llm._must_include(nodes)
        assert not any("None" in m for m in must), must


def test_linked_candidates_are_always_judged():
    linked = {"node_id": 9, "exact": False, "distance": None, "lex_terms": 0, "linked": True}
    assert pipeline._sure(linked, FLOOR) is False
    assert pipeline._sure({"node_id": 1, "exact": False, "distance": None, "lex_terms": 0}, FLOOR) is True


@requires_db
async def test_case_answer_carries_the_rule_it_followed(fake_mode, backfilled, admin_conn, monkeypatch):
    """사례만 찾은 질의에도, 체인의 사례가 근거로 적어 둔 규정 조문이 policy 이벤트(source=case)와 노드 표시로 온다."""
    async def troubleshoot(*a, **k):   # 적재된 규정에 따라 분류가 달라지지 않게 사례 질의로 고정
        return llm.IntentResult(intent="troubleshoot", reason="test")
    monkeypatch.setattr(pipeline.llm, "classify", troubleshoot)
    row = admin_conn.execute("""
        SELECT d.slug, c.article_label FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
        WHERE d.status = 'active' AND c.workspace_id IN (SELECT workspace_ancestors(2)) AND c.article_label ~ '^제[0-9]+조' ORDER BY c.id LIMIT 1""").fetchone()
    if not row:
        pytest.skip("ws2 에서 검색되는 조문이 없다")
    article = row["article_label"].split(" ")[0].split("(")[0]
    before = admin_conn.execute("SELECT attributes FROM log_nodes WHERE id = 113").fetchone()["attributes"]
    refs = [{"policy": row["slug"], "article": article, "relation": "follows", "note": "테스트"}]
    admin_conn.execute("UPDATE log_nodes SET attributes = coalesce(attributes, '{}'::jsonb) || jsonb_build_object('policy_refs', %s::jsonb) WHERE id = 113", (json.dumps(refs),))
    try:
        assert db_linked(2, row["slug"], article) == [113]
        blocks = await collect(pipeline.run(2, True, "VPN 접속할 때 E-403 오류가 반복되는데 예전에 어떻게 해결했어?"))
        d = dict(blocks)
        assert d["intent"]["intent"] == "troubleshoot"
        cited = [p for p in d["policy"] if p["source"] == "case"]
        assert len(cited) == 1 and cited[0]["article_label"] == row["article_label"]
        node = next(n for n in d["chain"]["nodes"] if n["id"] == 113)
        assert node["policy"] == [{"relation": "follows", "ref": cited[0]["ref"], "label": node["policy"][0]["label"], "note": "테스트", "topic": None,
                                  "revision": None, "from_version": None}]
        stages = [(b[1]["node"], b[1]["status"], b[1].get("detail")) for b in blocks if b[0] == "stage"]
        assert ("policy_rag", "start", "linked") in stages
        names = [b[0] for b in blocks]
        assert names.index("policy") < names.index("chain") < names.index("token")
        assert f"c:{cited[0]['chunk_id']}" in d["done"]["retrieved_ids"]
    finally:
        admin_conn.execute("UPDATE log_nodes SET attributes = %s::jsonb WHERE id = 113", (json.dumps(before or {}),))


def db_linked(ws, slug, article):
    with db.connect() as conn:
        return db.cases_linked_to(conn, ws, [(slug, article)])


def test_must_include_lists_cases_exceptions_and_entry_relation():
    def node(i, policy=None, entry=False, status="active"):
        return {"ref": f"N{i}", "id": i, "type": "fail", "status": status, "title": "t", "resolution_summary": None, "depth": 0, "policy": policy, "entry": entry}
    assert llm._must_include([]) == [] and llm._must_include([node(1, status="deprecated")]) == []
    must = llm._must_include([
        node(1, [{"relation": "follows", "ref": "P2", "label": "운영지침 제9조"}], entry=True),
        {**node(2, [{"relation": "exception", "ref": "P1", "label": "운영지침 제10조", "note": "당시 75%"}]), "near": True},
        node(3, [{"relation": "gap", "topic": "합산 상한"}]),
        node(4, [{"relation": "exception", "ref": "P1", "label": "x"}], status="deprecated"),
        node(5, [{"relation": "exception", "ref": "P3", "label": "운영지침 제17조"}]),
        node(6, [{"relation": "exception", "ref": "P4", "label": "운영지침 제23조"}])], searched={"P4"})
    assert must[0].startswith("해결 사례를 한 문장 이상")
    assert any("[N1]" in m and "[P2]" in m and "따른 규정" in m for m in must), "해결 줄이 없으면 진입 사례가 따른 규정을 밝힌다"
    assert any("[N2]" in m and "[P1]" in m and "당시 75%" in m for m in must), "진입 사례와 같은 줄기의 예외"
    assert any("[N6]" in m and "[P4]" in m for m in must), "질문으로 검색된 조문을 벗어난 예외"
    assert not any("N5" in m for m in must), "다른 가지의 예외는 강제하지 않는다"
    assert any("[N1]" in m and "[P2]" in m and "따른 규정" in m for m in must), "진입 사례가 따른 규정"
    assert not any("N3" in m or "N4" in m for m in must), "진입 노드가 아닌 '기준 없음'과 폐기 노드는 넣지 않는다"
    gap = llm._must_include([node(1, [{"relation": "gap", "topic": "합산 상한"}], entry=True)])
    assert any("규정에 기준이 없다" in m and "합산 상한" in m for m in gap)


def test_must_include_points_at_fix_lines_when_entry_is_a_problem():
    def node(i, typ, policy=None, entry=False, near=True):
        return {"ref": f"N{i}", "id": i, "type": typ, "status": "active", "title": "t", "resolution_summary": None, "depth": 0, "policy": policy,
                "entry": entry, "near": near}
    must = llm._must_include([
        node(1, "fail", [{"relation": "follows", "ref": "P1", "label": "제10조"}], entry=True),
        node(2, "success", [{"relation": "follows", "ref": "P2", "label": "제11조"}]),
        node(3, "success", near=False)])
    assert "[N2]" in must[0] and "N3" not in must[0], "진입 사례와 같은 줄기의 해결 줄만 지목한다"
    assert not any("[N1]" in m and "따른 규정" in m for m in must), "문제 줄이 따른 규정 대신"
    assert any("인용한 해결 줄" in m for m in must), "인용한 해결 줄의 규정을 밝히게 한다"


def test_must_include_asks_for_policy_marker_only_when_fix_lines_have_one():
    def node(i, typ, policy=None, entry=False):
        return {"ref": f"N{i}", "id": i, "type": typ, "status": "active", "title": "t", "resolution_summary": None, "depth": 0, "policy": policy,
                "entry": entry, "near": True}
    bare = llm._must_include([node(1, "fail", entry=True), node(2, "success")])
    assert not any("[P…]" in m for m in bare), "해결 줄에 근거 조문이 없으면 [P…] 를 붙이라고 하지 않는다"
    gap = llm._must_include([node(1, "fail", entry=True), node(2, "success", [{"relation": "gap", "topic": "합산 상한"}])])
    assert not any("[P…]" in m for m in gap) and any("기준이 없다" in m for m in gap), "기준 없음만 붙은 해결 줄"
    both = llm._must_include([node(1, "fail", entry=True), node(2, "success", [{"relation": "follows", "ref": "P2", "label": "제11조"}])])
    assert any("[P…]" in m for m in both)


def test_same_line_is_entry_with_ancestors_and_descendants():
    from app.graph import pipeline
    edges = [{"parent_id": 1, "child_id": 2}, {"parent_id": 2, "child_id": 3}, {"parent_id": 1, "child_id": 4}, {"parent_id": 4, "child_id": 5},
             {"parent_id": 3, "child_id": 6}, {"parent_id": 5, "child_id": 6}]
    assert pipeline._same_line(2, edges) == {1, 2, 3, 6}, "다른 가지(4·5)는 빠진다. 다중 부모 자손(6)은 든다"
    assert pipeline._same_line(1, edges) == {1, 2, 3, 4, 5, 6}


def test_case_is_optional_for_policy_questions():
    node = {"ref": "N1", "id": 1, "type": "fail", "status": "active", "title": "t", "resolution_summary": None, "depth": 0, "policy": None, "entry": True}
    assert "한 문장 이상" in llm._must_include([node])[0]
    soft = llm._must_include([node], case_required=False)[0]
    assert "먼저 규정으로 답한다" in soft and "상황이 다르면" in soft
