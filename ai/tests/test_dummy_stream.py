"""더미 스트림(AI_MODE=dummy) 단위 테스트. 이벤트 형식·순서·모드별 분기를 DB·Spring 없이 검증한다."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sse import EVENT_ORDER, sse
from tests.conftest import parse_blocks


def test_sse_format_and_unknown_event():
    assert sse("token", {"text": "a"}) == b'event: token\ndata: {"text": "a"}\n\n'
    with pytest.raises(ValueError):
        sse("bogus", {})
    assert "session" not in EVENT_ORDER  # session 은 Spring 이 송출한다


def test_dag_only_stream_has_no_policy_and_intent_troubleshoot(dummy_mode):
    client = TestClient(app)
    r = client.post("/ai/chat", json={"workspace_id": 1, "policy_enabled": False, "session_id": 7, "query": "E-403"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    blocks = parse_blocks(r.text)
    names = [b[0] for b in blocks]
    assert names[0] == "stage" and names[-1] == "done"
    assert "policy" not in names
    assert dict(blocks)["intent"] == {"intent": "troubleshoot", "mode": "dag_only"}
    done = blocks[-1][1]
    assert done["verified_ids"] == ["n:101", "n:106", "n:108"]
    assert set(done["latency"]) == {"retrieval_ms", "first_token_ms", "total_ms"}
    assert all(n in EVENT_ORDER for n in names)
    answer = "".join(b[1]["text"] for b in blocks if b[0] == "token")
    assert "[N1]" in answer and "[P1]" not in answer
    assert dict(blocks)["citations"]["verified"] == ["N1", "N2", "N3"]


def test_policy_stream_includes_policy_before_tokens(dummy_mode):
    client = TestClient(app)
    r = client.post("/ai/chat", json={"workspace_id": 2, "policy_enabled": True, "query": "VPN 신청 기준"})
    blocks = parse_blocks(r.text)
    names = [b[0] for b in blocks]
    assert names.index("policy") < names.index("token")
    assert names.index("chain") < names.index("token")
    assert dict(blocks)["intent"]["intent"] == "both"
    assert "c:vpn-guide-v21-a12-1" in blocks[-1][1]["verified_ids"]


def test_health_reports_mode_and_chain_summary(dummy_mode):
    client = TestClient(app)
    h = client.get("/ai/health").json()
    assert h["status"] == "ok" and h["mode"] == "dummy" and h["models"] == {}
    s = client.post("/ai/chain-summary", json={"nodes": [{"title": "A"}, {"title": "B"}], "edges": []}).json()
    assert s == {"summary": "A → B"}


def test_auto_mode_without_key_is_fake(monkeypatch):
    from app.config import ai_mode
    monkeypatch.setenv("AI_MODE", "auto")
    monkeypatch.delenv("LLM_PROFILE_DEFAULT_API_KEY", raising=False)
    assert ai_mode() == "fake"
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    assert ai_mode() == "openai"
