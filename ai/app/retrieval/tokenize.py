"""한국어 형태소 토크나이징. kiwipiepy 가 있으면 명사·동사 어간 중심으로, 없으면 정규식 폴백.
결과는 공백으로 이어 붙인 문자열이며 DB 의 tokens → tsv(생성 컬럼)로 들어간다. 인덱스 토큰과 질의 토큰이 같은 함수에서 나온다.

kiwi 제약: add_user_word 뒤 첫 분석에서 사전을 재빌드한다(실측 ≈1.4 s). 그래서 사용자 단어는 프로세스 전역 1회만 추가하고,
추가 직후 더미 분석으로 재빌드를 즉시 끝내며(ensure_terms), 분석은 락 안에서 돈다.
"""
from __future__ import annotations

import re
import threading
from functools import lru_cache

ERROR_CODE_RE = re.compile(r"\b(?:[A-Z]{1,4}-?\d{3,4}|HTTP\s?\d{3}|\d{3}\s?(?:Timeout|Error))\b", re.I)
KEEP_TAGS = {"NNG", "NNP", "NNB", "SL", "SN", "SH", "VV", "VA", "XR", "NR"}  # 명사·외래어·수사·동사/형용사 어간·어근

_lock = threading.RLock()
_added_terms: set[str] = set()


def normalize_code(raw: str) -> str:
    """'e-403'→'E-403', 'http504'/'HTTP  504'→'HTTP 504' (시드·정제 결과와 같은 표기로 exact 매칭)."""
    c = re.sub(r"\s+", " ", raw.strip().upper())
    return re.sub(r"^HTTP ?", "HTTP ", c)


def extract_error_code(text: str) -> str | None:
    m = ERROR_CODE_RE.search(text or "")
    return normalize_code(m.group(0)) if m else None


@lru_cache(maxsize=1)
def _kiwi():
    try:
        from kiwipiepy import Kiwi  # type: ignore
        return Kiwi()
    except Exception:
        return None


def ensure_terms(terms: list[str] | set[str] | None) -> int:
    """새 사용자 단어만 kiwi 에 추가하고 재빌드를 즉시 수행한다. 추가된 개수를 돌려준다."""
    kiwi = _kiwi()
    if not kiwi or not terms:
        return 0
    with _lock:
        new = [t for t in terms if t and t not in _added_terms]
        for t in new:
            try:
                kiwi.add_user_word(t, "NNP")
            except Exception:
                pass
            _added_terms.add(t)
        if new:
            kiwi.tokenize("사전 재빌드")  # 재빌드를 요청 처리 중이 아니라 여기서 끝낸다
        return len(new)


def warm_up(terms: list[str] | None = None) -> str:
    """기동 시 호출: 모델 로드 + 사용자 단어 전부 추가 + 첫 분석. 백엔드 이름을 돌려준다."""
    kiwi = _kiwi()
    if not kiwi:
        return "regex"
    with _lock:
        kiwi.tokenize("워밍업")
    ensure_terms(terms)
    return "kiwi"


class Tokenizer:
    def __init__(self, user_terms: list[str] | None = None):
        self.kiwi = _kiwi()
        self.terms = [t for t in (user_terms or []) if t]
        ensure_terms(self.terms)

    @property
    def backend(self) -> str:
        return "kiwi" if self.kiwi else "regex"

    def tokens(self, text: str) -> str:
        if not text:
            return ""
        out: list[str] = []
        # 에러 코드는 형태소 분석과 무관하게 토큰으로 보존한다 (E-403 이 'E' '403' 으로 쪼개지지 않게)
        codes = [normalize_code(c).replace(" ", "") for c in ERROR_CODE_RE.findall(text)]  # 토큰은 'http504' 한 덩어리
        if self.kiwi:
            with _lock:
                morphs = self.kiwi.tokenize(text)
            for tok in morphs:
                if tok.tag in KEEP_TAGS and len(tok.form) > 1:
                    out.append(tok.form.lower())
        else:
            for w in re.findall(r"[A-Za-z0-9\-]+|[가-힣]+", text):
                w = w.lower()
                if len(w) > 1:
                    out.append(w)
        return " ".join(dict.fromkeys([c.lower() for c in codes] + out))

    def nouns(self, text: str, limit: int = 8) -> list[str]:
        """키워드 후보(fake 정제용): 명사 빈도 상위."""
        if not text:
            return []
        if self.kiwi:
            with _lock:
                morphs = self.kiwi.tokenize(text)
            words = [t.form for t in morphs if t.tag in {"NNG", "NNP", "SL"} and len(t.form) > 1]
        else:
            words = [w for w in re.findall(r"[A-Za-z]{2,}|[가-힣]{2,}", text)]
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:limit]]
