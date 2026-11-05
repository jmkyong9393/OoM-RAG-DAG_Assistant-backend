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
    assert pipeline._article_keys(ps) == [("ops", "제5조"), ("ops", "별표 3")]
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
        assert node["policy"] == [{"relation": "follows", "ref": cited[0]["ref"], "label": node["policy"][0]["label"], "note": "테스트", "topic": None}]
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
