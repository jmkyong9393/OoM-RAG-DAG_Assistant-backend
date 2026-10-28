"""openai 모드 검증 — 실제 키 없이 OpenAI 호환 스텁 서버로 classify(JSON) · generate(스트리밍+usage) · refine(JSON) · embeddings 4경로를
실제 DB 와 함께 끝까지 돌린다. 키 발급 후에는 scripts/llm_smoke.py 로 같은 4경로를 실제 공급자에 1회 확인한다."""
import pytest

from app.config import profile
from app.graph import pipeline
from app.llm import client as llm
from app.retrieval.tokenize import Tokenizer
from tests.conftest import collect, requires_db
from tests.openai_stub import OpenAIStub, fake_embedding

pytestmark = requires_db


@pytest.fixture
def openai_mode(monkeypatch):
    with OpenAIStub() as stub:
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_LIGHT", "stub-light")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_ANSWER", "stub-answer")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_EMBEDDING_MODEL", "stub-embed")
        yield stub


async def test_classify_and_refine_use_json_mode(openai_mode):
    prof = profile()
    assert prof.has_key and prof.base_url == openai_mode.base_url
    u = llm.Usage()
    r = await llm.classify(prof, "VPN 규정상 E-403 에러는 어떻게 조치해?", "E-403", True, u)
    assert r.intent == "both" and r.reason == "stub"
    call = openai_mode.calls[-1]["body"]
    assert call["model"] == "stub-light" and call["response_format"] == {"type": "json_object"} and call["temperature"] == 0
    assert "error code detected: E-403" in call["messages"][1]["content"]
    rr = await llm.refine(prof, "HTTP 504 타임아웃", "커넥션 풀 확장으로 해결", Tokenizer(), u)
    assert rr.keywords == ["스텁", "키워드", "정제"]
    assert rr.error_code == "HTTP 504", "LLM 이 코드를 비우면 정규식 추출값으로 보완한다"
    assert u.prompt_tokens == 84 and u.completion_tokens == 14


async def test_pipeline_openai_streams_and_marks_unverified(openai_mode, backfilled):
    blocks = await collect(pipeline.run(2, True, "VPN E-403 에러가 나는데 규정상 어떻게 조치해야 해?"))
    d = dict(blocks)
    names = [b[0] for b in blocks]
    assert names[-1] == "done" and "error" not in names
    assert d["intent"]["intent"] == "both"
    tokens = [b[1]["text"] for b in blocks if b[0] == "token"]
    assert len(tokens) > 5 and "".join(tokens).startswith("스텁 답변입니다")
    assert d["citations"]["unverified"] == ["N99"]
    assert len(d["citations"]["verified"]) == 2
    done = d["done"]
    assert done["model"] == "stub-answer" and done["prompt_tokens"] >= 120 and done["completion_tokens"] >= 25
    assert done["unverified_ids"] == ["N99"] and len(done["verified_ids"]) == 2
    paths = [c["path"] for c in openai_mode.calls]
    assert paths.count("/v1/embeddings") == 1, "질의 임베딩은 한 번만"
    assert paths.count("/v1/chat/completions") == 2, "분류 1 + 생성 1"
    gen = [c for c in openai_mode.calls if c["body"].get("stream")][0]["body"]
    assert gen["stream_options"] == {"include_usage": True}
    assert "## 규정" in gen["messages"][1]["content"] and "## 해결 사례 체인" in gen["messages"][1]["content"]
    assert all(c["auth"] == "Bearer sk-test" for c in openai_mode.calls)


async def test_pipeline_openai_dag_only_skips_classify(openai_mode, backfilled):
    blocks = await collect(pipeline.run(1, False, "E-403 해결"))
    assert dict(blocks)["intent"] == {"intent": "troubleshoot", "mode": "dag_only"}
    paths = [c["path"] for c in openai_mode.calls]
    assert paths.count("/v1/chat/completions") == 1, "dag_only 는 분류 LLM 호출 없음"


async def test_worker_openai_writes_embedding(openai_mode, admin_conn):
    from app import worker
    row = admin_conn.execute("""
        INSERT INTO log_nodes (workspace_id, type, status, title, body, author_id, refine_status, source)
        VALUES (1, 'fail', 'active', '[test] 임베딩 확인', '스텁 공급자로 임베딩이 저장되는지 본다.', 2, 'pending', 'synthetic') RETURNING id""").fetchone()
    try:
        stats = await worker.run_once()
        assert stats["refined"] >= 1
        n = admin_conn.execute("SELECT refine_status, keywords, embedding_model, embedding IS NOT NULL AS has_vec, embedding <=> %s::vector AS dist FROM log_nodes WHERE id = %s",
                               ("[" + ",".join(f"{x:.7g}" for x in fake_embedding("[test] 임베딩 확인\n스텁 공급자로 임베딩이 저장되는지 본다.")) + "]", row["id"])).fetchone()
        assert n["refine_status"] == "done" and n["keywords"] == ["스텁", "키워드", "정제"]
        assert n["has_vec"] and n["embedding_model"] == "stub-embed"
        assert n["dist"] < 1e-4, "저장된 벡터가 공급자 응답과 같다"
        # 벡터 검색 경로: 같은 텍스트의 임베딩으로 질의하면 1위
        from app.retrieval import db
        with db.connect() as conn:
            hits = db.search_nodes(conn, 1, None, fake_embedding("[test] 임베딩 확인\n스텁 공급자로 임베딩이 저장되는지 본다."), None, 3)
        assert hits and hits[0]["node_id"] == row["id"]
    finally:
        admin_conn.execute("DELETE FROM log_nodes WHERE id = %s", (row["id"],))
        admin_conn.execute("DELETE FROM workspace_terms WHERE workspace_id = 1 AND source = 'auto' AND term IN ('스텁','키워드','정제')")


async def test_openai_mode_without_key_degrades_to_fake(openai_mode, monkeypatch, backfilled):
    """AI_MODE=openai 인데 키가 비어 있으면 죽지 않고 fake 경로로 내려가며 done.model 로 그 사실을 드러낸다."""
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "")
    blocks = await collect(pipeline.run(2, True, "VPN 규정"))
    names = [b[0] for b in blocks]
    assert names[-1] == "done" and dict(blocks)["done"]["model"] == "fake"
    assert not openai_mode.calls, "키가 없으면 공급자를 호출하지 않는다"
