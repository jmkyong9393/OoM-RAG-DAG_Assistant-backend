"""openai 모드 검증 — 실제 키 없이 OpenAI 호환 스텁 서버로 classify(JSON) · generate(스트리밍+usage) · refine(JSON) · embeddings 4경로를
실제 DB 와 함께 끝까지 돌린다. 키 발급 후에는 scripts/llm_smoke.py 로 같은 4경로를 실제 공급자에 1회 확인한다."""
import pytest

from app import config
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
        monkeypatch.delenv("LLM_PROFILE_DEFAULT_API_KEY_BACKUP", raising=False)
        monkeypatch.setattr(config, "_on_backup", set())   # 예비 키로 넘어간 상태는 프로세스에 남는다 — 테스트마다 처음부터
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_LIGHT", "stub-light")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_ANSWER", "stub-answer")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_EMBEDDING_MODEL", "stub-embed")
        # 로컬 DB 에는 실제 모델로 만든 벡터가 있을 수 있다 — 모델 불일치 방어는 따로 본다
        monkeypatch.setattr(pipeline.db, "embedding_mismatch", lambda conn, model: False)
        yield stub


async def test_classify_and_refine_use_json_mode(openai_mode):
    prof = profile()
    assert prof.has_key and prof.base_url == openai_mode.base_url
    u = llm.Usage()
    # 힌트가 있으면 규칙으로 끝나 LLM 을 부르지 않는다
    n = len(openai_mode.calls)
    r = await llm.classify(prof, "VPN 규정상 E-403 에러는 어떻게 조치해?", "E-403", True, u)
    assert r.intent == "both" and r.reason.startswith("rule:") and len(openai_mode.calls) == n
    # 힌트가 없는 질문만 LLM(JSON 모드)로 간다
    r = await llm.classify(prof, "요즘 VPN 이 왜 이래?", None, True, u)
    assert r.intent == "troubleshoot" and r.reason == "llm: stub"   # 스텁은 '규정' 없는 질문을 troubleshoot 로 답한다
    call = openai_mode.calls[-1]["body"]
    assert call["model"] == "stub-light" and call["response_format"] == {"type": "json_object"} and call["temperature"] == 0
    assert "error code detected" not in call["messages"][1]["content"]
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
    assert paths.count("/v1/chat/completions") == 2, "규정 재순위 1 + 생성 1 (분류는 '에러'+'규정' 힌트로 규칙 처리, LLM 호출 없음)"
    rr = [c["body"] for c in openai_mode.calls if c["path"] == "/v1/chat/completions" and not c["body"].get("stream")]
    assert len(rr) == 1 and rr[0]["model"] == "stub-light" and "re-rank" in rr[0]["messages"][0]["content"]
    gen = [c for c in openai_mode.calls if c["body"].get("stream")][0]["body"]
    assert gen["stream_options"] == {"include_usage": True}
    assert gen["max_tokens"] == 400 and done["truncated"] is False, "기본 상한 400 이 요청에 실리고, 스텁 답변(어절 10여 개)은 잘리지 않는다"
    assert "## 규정" in gen["messages"][1]["content"] and "## 해결 사례 체인" in gen["messages"][1]["content"]
    assert all(c["auth"] == "Bearer sk-test" for c in openai_mode.calls)


async def test_pipeline_without_rerank_calls_only_generation(openai_mode, backfilled, monkeypatch):
    """POLICY_RERANK=0 이면 재순위 호출 없이 검색 순서로 답한다."""
    monkeypatch.setenv("POLICY_RERANK", "0")
    blocks = await collect(pipeline.run(2, True, "VPN E-403 에러가 나는데 규정상 어떻게 조치해야 해?"))
    assert [b[0] for b in blocks][-1] == "done"
    assert [c["path"] for c in openai_mode.calls].count("/v1/chat/completions") == 1


async def test_pipeline_skips_vector_search_when_stored_vectors_are_from_another_model(openai_mode, backfilled, monkeypatch):
    """저장된 벡터가 다른 모델의 것이면 질의를 임베딩하지 않고 어휘 검색만으로 답한다."""
    monkeypatch.setattr(pipeline.db, "embedding_mismatch", lambda conn, model: True)
    blocks = await collect(pipeline.run(2, True, "VPN E-403 에러가 나는데 규정상 어떻게 조치해야 해?"))
    names = [b[0] for b in blocks]
    assert names[-1] == "done" and "error" not in names and "policy" in names
    assert [c["path"] for c in openai_mode.calls].count("/v1/embeddings") == 0


async def test_pipeline_answers_lexically_when_query_embedding_fails(openai_mode, backfilled, monkeypatch):
    def boom(prof, text):
        raise RuntimeError("embeddings down")
    monkeypatch.setattr(pipeline.embed, "embed_one", boom)
    blocks = await collect(pipeline.run(2, True, "VPN E-403 에러가 나는데 규정상 어떻게 조치해야 해?"))
    names = [b[0] for b in blocks]
    assert names[-1] == "done" and "error" not in names and "policy" in names


async def test_pipeline_openai_marks_truncated_answer(openai_mode, backfilled, monkeypatch):
    """상한에 걸리면(finish_reason=length) 잘렸다는 안내 토큰을 덧붙이고 done.truncated 로 알린다. 상한 0 은 무제한."""
    monkeypatch.setenv("ANSWER_MAX_TOKENS", "3")
    blocks = await collect(pipeline.run(2, True, "VPN 규정상 승인 절차"))
    d = dict(blocks)
    tokens = [b[1]["text"] for b in blocks if b[0] == "token"]
    assert d["done"]["truncated"] is True and tokens[-1] == pipeline.TRUNCATED_NOTE and len(tokens) == 4
    assert openai_mode.calls[-1]["body"]["max_tokens"] == 3
    monkeypatch.setenv("ANSWER_MAX_TOKENS", "0")
    blocks = await collect(pipeline.run(2, True, "VPN 규정상 승인 절차"))
    assert dict(blocks)["done"]["truncated"] is False and "max_tokens" not in openai_mode.calls[-1]["body"]


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


NO_CREDIT = {"message": "You have no credits remaining.", "type": "insufficient_quota", "code": "credit_balance_exhausted"}


async def test_backup_key_takes_over_when_the_primary_is_rejected(openai_mode, monkeypatch):
    """기본 키가 잔액 소진 · 인증 실패로 거절되면 같은 요청을 예비 키로 한 번 다시 보내고, 그 뒤로는 예비 키만 쓴다."""
    import httpx

    from app.retrieval import embed
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-empty")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY_BACKUP", "sk-backup")
    openai_mode.reject("sk-empty", 429, NO_CREDIT)
    prof, u = profile(), llm.Usage()
    assert (await llm.classify(prof, "요즘 VPN 이 왜 이래?", None, True, u)).reason == "llm: stub"
    assert [c["auth"] for c in openai_mode.calls] == ["Bearer sk-empty", "Bearer sk-backup"]
    assert embed.embed_one(prof, "질문") is not None
    assert "".join([p async for p in llm._chat_stream(prof, "m", "s", "u", u)]).startswith("스텁 답변")
    assert [c["auth"] for c in openai_mode.calls[2:]] == ["Bearer sk-backup"] * 2, "한 번 넘어가면 기본 키를 다시 두드리지 않는다"
    # 스트리밍 · 임베딩이 첫 호출이어도 넘어간다 (인증 실패 401 도 같다)
    for first in ("stream", "embed"):
        monkeypatch.setattr(config, "_on_backup", set())
        openai_mode.reject("sk-empty", 401, {"message": "Incorrect API key provided"})
        n = len(openai_mode.calls)
        if first == "stream":
            assert "".join([p async for p in llm._chat_stream(prof, "m", "s", "u", u)]).startswith("스텁 답변")
        else:
            assert embed.embed_one(prof, "질문") is not None
        assert [c["auth"] for c in openai_mode.calls[n:]] == ["Bearer sk-empty", "Bearer sk-backup"], first
    # 잠깐의 속도 제한(429)은 키 문제가 아니다 — 넘어가지 않고 오류 그대로
    monkeypatch.setattr(config, "_on_backup", set())
    openai_mode.reject("sk-empty", 429, {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"})
    n = len(openai_mode.calls)
    with pytest.raises(httpx.HTTPStatusError):
        await llm._chat_json(prof, "m", "classify", "u", u)
    assert [c["auth"] for c in openai_mode.calls[n:]] == ["Bearer sk-empty"] and not config._on_backup


async def test_backup_key_is_used_when_the_primary_is_blank_and_errors_surface_without_one(openai_mode, monkeypatch):
    import httpx
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY_BACKUP", "sk-backup")
    prof, u = profile(), llm.Usage()
    assert prof.has_key and config.api_key_of(prof) == "sk-backup"
    await llm._chat_json(prof, "m", "classify", "u", u)
    assert [c["auth"] for c in openai_mode.calls] == ["Bearer sk-backup"]
    # 예비 키가 없으면 지금처럼 오류가 그대로 올라온다
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-empty")
    monkeypatch.delenv("LLM_PROFILE_DEFAULT_API_KEY_BACKUP")
    openai_mode.reject("sk-empty", 429, NO_CREDIT)
    with pytest.raises(httpx.HTTPStatusError):
        await llm._chat_json(profile(), "m", "classify", "u", u)
