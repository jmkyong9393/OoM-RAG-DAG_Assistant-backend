"""긴 글 · 모양이 다른 오류 코드 · 제어 문자 — 저장된 코드로 질문의 코드를 찾는 것, 임베딩 입력을 줄여 다시 보내는 것, 변환본의 널 문자."""
import httpx
import pytest

from app.ingest import convert
from app.retrieval import db, embed


class Conn:
    """log_nodes 의 오류 코드 목록만 돌려주는 대역."""

    def __init__(self, codes):
        self.codes = codes

    def execute(self, sql, params):
        assert "error_code" in sql and params == (7,)
        return self

    def fetchall(self):
        return [{"error_code": c} for c in self.codes]


CODES = ["ORA-12154", "OOMKilled", "E-403", "HTTP 504", "ECONNREFUSED", "ORA-121"]


@pytest.mark.parametrize("query,expected", [
    ("결재 올릴 때 ORA-12154 나는 거 어떻게 해결했어?", "ORA-12154"),   # 숫자 다섯 자리 — 정규식 밖. 더 짧은 ORA-121 이 아니라 긴 쪽
    ("oomkilled 났을 때 어떻게 했어", "OOMKilled"),                       # 대소문자를 가리지 않고, 저장된 표기를 돌려준다
    ("ECONNREFUSED가 계속 떠요", "ECONNREFUSED"),                        # 한글이 바로 붙어도 찾는다
    ("XECONNREFUSEDX 는 뭐야", None),                                    # 다른 낱말의 일부면 아니다
    ("배포 창은 언제야?", None),
])
def test_known_code_finds_stored_codes_in_the_question(query, expected):
    assert db.known_code(Conn(CODES), 7, query) == expected


def test_known_code_with_no_codes():
    assert db.known_code(Conn([]), 7, "ORA-12154 왜 나?") is None


def too_long(limit):
    def fake(profile, texts):
        if len(texts[0]) > limit:
            raise httpx.HTTPStatusError("too many tokens", request=None, response=httpx.Response(400))
        return [[float(len(texts[0]))]]
    return fake


def test_embed_one_halves_the_input_until_it_fits(monkeypatch):
    monkeypatch.setattr(embed, "embed", too_long(2000))
    v = embed.embed_one(None, "가" * 50000)
    assert v == [float(embed.EMBED_CHARS // 4)]   # 6,000 → 3,000 → 1,500


def test_embed_one_short_text_is_sent_as_is(monkeypatch):
    monkeypatch.setattr(embed, "embed", too_long(2000))
    assert embed.embed_one(None, "짧은 글") == [4.0]


def test_embed_one_gives_up_and_other_errors_pass_through(monkeypatch):
    monkeypatch.setattr(embed, "embed", too_long(0))   # 아무리 줄여도 400
    with pytest.raises(httpx.HTTPStatusError):
        embed.embed_one(None, "가" * 50000)

    def unauthorized(profile, texts):
        raise httpx.HTTPStatusError("bad key", request=None, response=httpx.Response(401))
    monkeypatch.setattr(embed, "embed", unauthorized)
    with pytest.raises(httpx.HTTPStatusError):
        embed.embed_one(None, "가" * 50000)


def test_normalize_drops_control_characters_but_keeps_lines_and_tabs():
    out = convert.normalize("## 제1조 (널)\n가\x00나\x07다\n| 가 |\t나 |\n")
    assert out == "## 제1조 (널)\n가나다\n| 가 |\t나 |\n"
