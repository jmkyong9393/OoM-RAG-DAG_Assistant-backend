"""환경 설정. LLM 공급자는 '프로필' 단위로 env 에서 읽는다 (개발문서 34 §4):
LLM_PROFILE_<NAME>_BASE_URL / _API_KEY / _MODEL_LIGHT / _MODEL_ANSWER / _EMBEDDING_MODEL.
워크스페이스 settings.llm_profile 이 프로필 이름을 가리키며, 없으면 'default'.

AI_MODE:
  dummy  — 고정 이벤트 (DB·LLM 불필요, E1 스파이크용)
  fake   — 실제 DB 검색 + 규칙 기반 분류·템플릿 답변·정규식 정제 (API 키 불필요, 시연 폴백·테스트)
  openai — OpenAI 호환 API 호출
  auto   — default 프로필에 키가 있으면 openai, 없으면 fake (기본)
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LlmProfile:
    name: str
    base_url: str
    api_key: str
    model_light: str
    model_answer: str
    embedding_model: str
    embedding_dim: int = 1536

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def profile(name: str = "default") -> LlmProfile:
    p = f"LLM_PROFILE_{name.upper()}_"
    return LlmProfile(
        name=name,
        base_url=os.environ.get(p + "BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key=os.environ.get(p + "API_KEY", ""),
        model_light=os.environ.get(p + "MODEL_LIGHT", "gpt-4o-mini"),
        model_answer=os.environ.get(p + "MODEL_ANSWER", "gpt-4o-mini"),
        embedding_model=os.environ.get(p + "EMBEDDING_MODEL", "text-embedding-3-small"),
    )


def ai_mode() -> str:
    mode = os.environ.get("AI_MODE", "auto").lower()
    if mode == "auto":
        return "openai" if profile().has_key else "fake"
    return mode


def db_url() -> str:
    return os.environ.get("AI_DB_URL", "postgresql://oom_ai:oom_ai@localhost:15432/oom")


def worker_poll_sec() -> float:
    return float(os.environ.get("WORKER_POLL_SEC", "3"))
