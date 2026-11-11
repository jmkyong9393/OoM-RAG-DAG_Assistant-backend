"""/ai/chain-summary — openai 모드는 체인 기록을 LLM(답변 모델)으로 한 단락 요약, 키 없음·실패는 제목 연결. DB 불필요."""
import asyncio

import httpx

from app.llm.client import _chain_header, _chain_tree, _clip
from app.main import app
from tests.openai_stub import OpenAIStub

NODES = [
    {"id": 1, "type": "fail", "status": "active", "title": "VPN 접속 시 E-403 반복", "created_at": "2026-11-03T09:00:00+09:00"},
    {"id": 2, "type": "fail", "status": "active", "title": "토큰 갱신 배치 중단", "is_cause": True, "created_at": "2026-11-03T10:00:00+09:00"},
    {"id": 3, "type": "success", "status": "active", "title": "배치 재기동", "body": "배치를 다시 띄워\n해결",
     "resolves_root": True, "created_at": "2026-11-04T09:00:00+09:00"},
    {"id": 4, "type": "fail", "status": "deprecated", "title": "클라이언트 재설치", "created_at": "2026-11-03T11:00:00+09:00"},
]
EDGES = [{"parent_id": 1, "child_id": 2}, {"parent_id": 2, "child_id": 3, "reason": "원인 조치"}, {"parent_id": 1, "child_id": 4}]


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_chain_is_rendered_as_indented_tree_in_order():
    assert _chain_tree(NODES, EDGES) == [
        "- [실패] VPN 접속 시 E-403 반복 (11/03)",
        "  - [실패·원인] 토큰 갱신 배치 중단 (11/03)",
        "    - [성공·해결] 배치 재기동 (11/04) — 배치를 다시 띄워 해결 ← 이어 붙인 이유: 원인 조치",
        "  - [실패·폐기] 클라이언트 재설치 (11/03)",
    ]


def test_node_with_two_parents_is_written_once():
    nodes = [{"id": 1, "type": "fail", "title": "A"}, {"id": 2, "type": "ref", "title": "B"}, {"id": 3, "type": "success", "title": "C"}]
    edges = [{"parent_id": 1, "child_id": 2}, {"parent_id": 1, "child_id": 3}, {"parent_id": 2, "child_id": 3}]
    assert _chain_tree(nodes, edges) == ["- [실패] A", "  - [참고] B", "    - [성공] C", "  - (위와 같은 기록) C"]


def test_state_is_never_written_and_reference_root_is_marked_as_memo():
    done = [{**NODES[0], "resolved_at": "2026-11-05T09:00:00+09:00"}] + NODES[1:]
    assert _chain_tree(done, EDGES)[0] == "- [실패] VPN 접속 시 E-403 반복 (11/03)"   # 완료 처리 여부는 요약에 쓰지 않는다(화면이 표시)
    nodes = [{"id": 1, "type": "ref", "status": "active", "title": "로컬 개발 DB 초기화 메모", "body": "DB 볼륨을 지우고 다시 시드"}]
    assert _chain_tree(nodes, []) == ["- [참고] 로컬 개발 DB 초기화 메모 — DB 볼륨을 지우고 다시 시드"]
    assert _chain_header(nodes, []) == "[참고 메모]"   # 데이터 바로 위에 적는다(설명 없이 — 요약에 옮겨지지 않게)
    assert _chain_header(NODES, EDGES) == "[기록]"   # 장애 건(루트 실패)은 그대로


async def test_openai_mode_summarizes_with_answer_model(monkeypatch):
    with OpenAIStub() as stub:
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_ANSWER", "stub-answer")
        async with _client() as client:
            s = (await client.post("/ai/chain-summary", json={"nodes": NODES, "edges": EDGES, "total": 50})).json()
        assert s == {"summary": "스텁 체인 요약입니다.", "code": None, "model": "stub-answer", "prompt_tokens": 42, "completion_tokens": 7}   # 비용 확인용
        body = stub.calls[-1]["body"]
        assert body["model"] == "stub-answer" and body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 600
        assert "흐름" in body["messages"][0]["content"]
        assert body["messages"][1]["content"].startswith("[기록]\n(기록 50건 중 4건만 — 해결 · 원인 경로를 먼저 골랐다)\n- [실패] VPN 접속 시 E-403 반복")


async def test_summary_moves_to_the_backup_key_when_the_primary_is_out_of_credit(monkeypatch):
    from app import config
    with OpenAIStub() as stub:
        monkeypatch.setattr(config, "_on_backup", set())
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-empty")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY_BACKUP", "sk-backup")
        stub.reject("sk-empty", 429, {"message": "no credits", "type": "insufficient_quota", "code": "credit_balance_exhausted"})
        async with _client() as client:
            s = (await client.post("/ai/chain-summary", json={"nodes": NODES, "edges": EDGES})).json()
        assert s["code"] is None and s["summary"] == "스텁 체인 요약입니다."
        assert [c["auth"] for c in stub.calls] == ["Bearer sk-empty", "Bearer sk-backup"]


def test_long_summary_is_clipped_at_a_sentence_end():
    assert _clip("짧습니다.") == "짧습니다."
    long = "가" * 250 + "습니다. " + "나" * 300 + "습니다. " + "다" * 300 + "습니다."
    assert _clip(long) == "가" * 250 + "습니다. " + "나" * 300 + "습니다."   # 600자 안의 마지막 문장까지
    assert _clip("가" * 700) == "가" * 600 + "…"   # 문장 끝이 없으면 글자로


TITLES = "VPN 접속 시 E-403 반복 → 토큰 갱신 배치 중단 → 배치 재기동 → 클라이언트 재설치"


def _openai(monkeypatch, base_url="http://127.0.0.1:9/v1"):   # 기본은 닫힌 포트 — 연결 실패
    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", base_url)
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")


async def _summary(**kw):
    async with _client() as client:
        return (await client.post("/ai/chain-summary", json={"nodes": NODES, "edges": EDGES, **kw})).json()


async def test_connection_failure_is_timeout_kind(monkeypatch):
    _openai(monkeypatch)
    assert await _summary() == {"summary": TITLES, "code": "AI_TIMEOUT"}


async def test_slow_llm_is_cut_at_timeout(monkeypatch):
    from app import main
    _openai(monkeypatch)

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "늦은 요약"
    monkeypatch.setattr(main.llm, "summarize_chain", slow)
    monkeypatch.setattr(main, "CHAIN_SUMMARY_TIMEOUT_SEC", 0.05)
    assert await _summary() == {"summary": TITLES, "code": "AI_TIMEOUT"}


async def test_empty_llm_answer_is_bad_output(monkeypatch):
    from app import main
    _openai(monkeypatch)

    async def empty(*a, **k):
        return None
    monkeypatch.setattr(main.llm, "summarize_chain", empty)
    assert await _summary() == {"summary": TITLES, "code": "AI_BAD_OUTPUT"}


async def test_rate_limit_kind(monkeypatch):
    from app import main
    _openai(monkeypatch)

    async def limited(*a, **k):
        req = httpx.Request("POST", "http://x/v1/chat/completions")
        raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))
    monkeypatch.setattr(main.llm, "summarize_chain", limited)
    assert await _summary() == {"summary": TITLES, "code": "AI_RATE_LIMIT"}


async def test_no_key_is_disabled_not_a_failure(monkeypatch):
    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "")
    assert await _summary() == {"summary": TITLES, "code": "AI_DISABLED"}
