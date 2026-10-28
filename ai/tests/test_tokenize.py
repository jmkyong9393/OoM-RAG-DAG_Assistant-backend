"""토크나이저 단위 테스트. kiwi 가 설치된 환경과 정규식 폴백 둘 다 검증한다."""
import pytest

from app.retrieval import tokenize
from app.retrieval.tokenize import Tokenizer, extract_error_code


@pytest.mark.parametrize("text,expected", [
    ("VPN 접속 시 E-403 반복 발생", "E-403"),
    ("FAQ 연동 API 타임아웃(504) 발생, HTTP 504 반환", "HTTP 504"),
    ("http504 가 떴다", "HTTP 504"),
    ("임베딩 배치 HTTP 429 반복 실패", "HTTP 429"),
    ("제12조 원격접속 신청 기준", None),
    ("2026년 10월 배포", None),
])
def test_extract_error_code_normalizes(text, expected):
    assert extract_error_code(text) == expected


def test_kiwi_tokens_keep_nouns_and_error_code():
    tok = Tokenizer()
    if tok.backend != "kiwi":
        pytest.skip("kiwipiepy not installed")
    t = tok.tokens("VPN 접속 시 E-403 반복 발생. 토큰 갱신 배치가 중단된 것을 확인하고 배치 주기를 30분으로 조정함.")
    parts = t.split()
    assert "e-403" in parts            # 에러 코드는 형태소 분석과 무관하게 보존
    assert "vpn" in parts
    assert "토큰" in parts and "배치" in parts and "접속" in parts
    assert "시" not in parts            # 1글자 조사·의존명사는 버린다
    assert len(parts) == len(set(parts))  # 중복 제거


def test_user_terms_are_kept_as_one_token():
    tok = Tokenizer(["게이트웨이인증"])
    if tok.backend != "kiwi":
        pytest.skip("kiwipiepy not installed")
    assert "게이트웨이인증" in tok.tokens("게이트웨이인증 로그를 확인한다").split()


def test_regex_fallback_when_kiwi_missing(monkeypatch):
    monkeypatch.setattr(tokenize, "_kiwi", lambda: None)
    tok = Tokenizer(["무시됨"])
    assert tok.backend == "regex"
    parts = tok.tokens("VPN 접속 E-403 반복").split()
    assert parts[0] == "e-403" and "vpn" in parts and "접속" in parts
    assert tok.nouns("배치 배치 토큰", limit=2) == ["배치", "토큰"]


def test_query_and_index_share_tokenizer():
    tok = Tokenizer()
    idx = set(tok.tokens("제12조 (원격접속 신청) 사외에서 사내망에 접속하려는 임직원은 사전에 신청하여야 한다").split())
    q = set(tok.tokens("원격접속 신청은 어떻게 하나요").split())
    assert idx & q, "질의 토큰과 인덱스 토큰이 하나도 겹치지 않으면 어휘 검색이 불가능하다"
