"""임베딩. OpenAI 호환 /embeddings 를 httpx 로 호출한다. fake 모드(키 없음)에서는 None 을 돌려 어휘 검색만으로 동작한다."""
from __future__ import annotations

import httpx

from app.config import LlmProfile, ai_mode


def embed(profile: LlmProfile, texts: list[str]) -> list[list[float]] | None:
    if ai_mode() != "openai" or not profile.has_key or not texts:
        return None
    r = httpx.post(f"{profile.base_url}/embeddings", timeout=60,
                   headers={"Authorization": f"Bearer {profile.api_key}"},
                   json={"model": profile.embedding_model, "input": texts})
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def embed_one(profile: LlmProfile, text: str) -> list[float] | None:
    v = embed(profile, [text])
    return v[0] if v else None
