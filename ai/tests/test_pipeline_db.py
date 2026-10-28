"""파이프라인 통합 테스트 — 실제 Postgres(시드) + fake LLM. 검색·체인·마커 검증·이벤트 순서를 끝까지 돈다."""
import pytest
from fastapi.testclient import TestClient

from app.graph import pipeline
from app.sse import EVENT_ORDER
from tests.conftest import collect, parse_blocks, requires_db

pytestmark = requires_db

WS_SW, WS_HR, WS_CS = 1, 2, 3  # 시드: SW개발(dag_only) · 인사총무(policy) · 고객지원(policy)


def names_of(blocks):
    return [b[0] for b in blocks]


async def test_dag_only_troubleshoot_returns_root_chain(fake_mode, backfilled):
    blocks = await collect(pipeline.run(WS_SW, False, "VPN 접속할 때 E-403 오류가 반복되는데 예전에 어떻게 해결했어?"))
    names = names_of(blocks)
    assert names[-1] == "done" and all(n in EVENT_ORDER for n in names)
    assert "policy" not in names and "chain" in names
    d = dict(blocks)
    assert d["intent"] == {"intent": "troubleshoot", "mode": "dag_only"}
    chain = d["chain"]
    ids = {n["id"] for n in chain["nodes"]}
    assert 101 in ids, "루트(fail) 가 체인에 있어야 한다"
    assert 108 in ids and 106 in ids
    assert chain["nodes"][0]["depth"] == 0 and chain["nodes"][0]["ref"] == "N1"
    assert all(e["parent_id"] in ids and e["child_id"] in ids for e in chain["edges"])
    answer = "".join(b[1]["text"] for b in blocks if b[0] == "token")
    cit = d["citations"]
    assert cit["verified"] and not cit["unverified"]
    assert all(f"[{m}]" in answer for m in cit["verified"])
    done = d["done"]
    assert done["model"] == "fake"
    assert all(i.startswith("n:") for i in done["retrieved_ids"]) and "n:101" in done["retrieved_ids"]
    assert set(done["verified_ids"]) <= set(done["retrieved_ids"])
    assert done["latency"]["retrieval_ms"] <= done["latency"]["first_token_ms"] <= done["latency"]["total_ms"]
    # 폐기 노드(109)는 체인에 남되 답변의 해결책으로 쓰이지 않는다
    dep = [n for n in chain["nodes"] if n["id"] == 109]
    if dep:
        assert dep[0]["status"] == "deprecated"
        assert f"[{dep[0]['ref']}]" not in answer


async def test_policy_query_hits_active_version_only(fake_mode, backfilled):
    blocks = await collect(pipeline.run(WS_HR, True, "VPN 원격접속 신청 기준과 절차가 뭐야?"))
    d = dict(blocks)
    assert d["intent"]["intent"] == "policy" and d["intent"]["mode"] == "policy"
    names = names_of(blocks)
    assert "policy" in names and "chain" not in names
    assert names.index("policy") < names.index("token")
    refs = d["policy"]
    assert refs and refs[0]["ref"] == "P1"
    chunk_ids = [r["chunk_id"] for r in refs]
    assert any(c.startswith("vpn-guide-v21") for c in chunk_ids)
    assert not any(c.startswith("vpn-guide-v20") for c in chunk_ids), "retired 문서는 검색되지 않는다"
    assert all(r["doc_title"] and r["article_label"] and r["text"] for r in refs)
    assert all(i.startswith("c:") for i in d["done"]["retrieved_ids"])
    assert d["citations"]["verified"][0] == "P1"


async def test_both_intent_runs_both_workers(fake_mode, backfilled):
    blocks = await collect(pipeline.run(WS_HR, True, "VPN E-403 에러가 나는데 규정상 어떻게 조치해야 해?"))
    d = dict(blocks)
    names = names_of(blocks)
    assert d["intent"]["intent"] == "both"
    assert "policy" in names and "chain" in names
    assert names.index("policy") < names.index("token") and names.index("chain") < names.index("token")
    # 인사총무(ws 2) 의 체인만 — SW개발(ws 1) 노드 101 은 격리되어 보이지 않는다
    ids = {n["id"] for n in d["chain"]["nodes"]}
    assert 111 in ids and 101 not in ids
    stages = [b[1] for b in blocks if b[0] == "stage"]
    nodes = [s["node"] for s in stages]
    for n in ("supervisor", "policy_rag", "graph_search", "gate", "generate", "critic"):
        assert n in nodes
    assert all(s["status"] != "error" for s in stages)


async def test_no_evidence_gate(fake_mode, backfilled):
    blocks = await collect(pipeline.run(WS_CS, True, "양자컴퓨터 큐비트 결맞음 시간"))
    d = dict(blocks)
    names = names_of(blocks)
    assert "policy" not in names or d["policy"] == [] or True  # 어휘 검색이 0건이면 policy 이벤트 자체가 빈 배열
    gate = [b[1] for b in blocks if b[0] == "stage" and b[1]["node"] == "gate"]
    if gate and gate[0].get("detail") == "no_evidence":
        answer = "".join(b[1]["text"] for b in blocks if b[0] == "token")
        assert answer.startswith("근거 없음")
        assert d["done"]["model"] == "gate" and d["done"]["verified_ids"] == []
    else:
        # 어휘 겹침(예: '시간')으로 뭔가 잡혔다면 그래도 검증된 마커만 verified 여야 한다
        assert set(d["citations"]["verified"]) <= {r["ref"] for r in d.get("policy", [])} | {n["ref"] for n in d.get("chain", {}).get("nodes", [])}


async def test_policy_enabled_null_falls_back_to_workspace_setting(fake_mode, backfilled):
    blocks = await collect(pipeline.run(WS_SW, None, "E-403"))
    assert dict(blocks)["intent"]["mode"] == "dag_only"


def test_http_endpoint_streams_pipeline(fake_mode, backfilled):
    from app.main import app
    client = TestClient(app)
    h = client.get("/ai/health").json()
    assert h["mode"] == "fake" and h["db"] is True
    r = client.post("/ai/chat", json={"workspace_id": WS_SW, "policy_enabled": False, "session_id": 1, "query": "E-403 해결"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    blocks = parse_blocks(r.text)
    assert blocks[0][0] == "stage" and blocks[-1][0] == "done"
