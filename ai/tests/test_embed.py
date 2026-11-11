"""임베딩 호출 — 차원 지정과 일시 오류 재시도. 네트워크 없이 httpx.post 를 바꿔 끼운다."""
import httpx
import pytest

from app.config import LlmProfile
from app.retrieval import embed


class Resp:
    def __init__(self, status: int, n: int = 1):
        self.status_code, self._n = status, n
        self.text = '{"error": {"type": "requests", "code": "rate_limit_exceeded"}}' if status >= 400 else ""   # 키 문제가 아닌 일시 오류

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return {"data": [{"index": i, "embedding": [0.0, 1.0]} for i in range(self._n)]}


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setattr(embed.time, "sleep", lambda s: None)
    sent: list[dict] = []
    return sent


def prof(model: str) -> LlmProfile:
    return LlmProfile("default", "http://stub/v1", "sk-test", "light", "answer", model)


def test_dimensions_sent_only_for_models_that_accept_them(calls, monkeypatch):
    def post(url, **kw):
        calls.append(kw["json"])
        return Resp(200)
    monkeypatch.setattr(embed.httpx, "post", post)
    embed.embed(prof("text-embedding-3-large"), ["a"])
    embed.embed(prof("other-embed"), ["a"])
    assert calls[0]["dimensions"] == 1536, "큰 모델은 스키마 차원으로 줄여 받는다"
    assert "dimensions" not in calls[1]


def test_transient_error_is_retried_once(calls, monkeypatch):
    seq = iter([Resp(520), Resp(200)])
    monkeypatch.setattr(embed.httpx, "post", lambda url, **kw: (calls.append(kw["json"]), next(seq))[1])
    assert embed.embed_one(prof("text-embedding-3-large"), "a") == [0.0, 1.0]
    assert len(calls) == 2


def test_second_failure_is_raised(calls, monkeypatch):
    monkeypatch.setattr(embed.httpx, "post", lambda url, **kw: (calls.append(kw["json"]), Resp(503))[1])
    with pytest.raises(httpx.HTTPStatusError):
        embed.embed(prof("text-embedding-3-large"), ["a"])
    assert len(calls) == 2


def test_connection_error_is_retried_once(calls, monkeypatch):
    def post(url, **kw):
        calls.append(kw["json"])
        if len(calls) == 1:
            raise httpx.ConnectError("reset")
        return Resp(200)
    monkeypatch.setattr(embed.httpx, "post", post)
    assert embed.embed_one(prof("text-embedding-3-large"), "a") == [0.0, 1.0]
