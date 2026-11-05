"""임베딩. OpenAI 호환 /embeddings 를 httpx 로 호출한다. fake 모드(키 없음)에서는 None 을 돌려 어휘 검색만으로 동작한다."""
from __future__ import annotations

import time

import httpx

from app.config import LlmProfile, ai_mode


def embed(profile: LlmProfile, texts: list[str]) -> list[list[float]] | None:
    if ai_mode() != "openai" or not profile.has_key or not texts:
        return None
    body: dict = {"model": profile.embedding_model, "input": texts}
    if profile.embedding_model.startswith("text-embedding-3"):   # 차원을 줄여 받을 수 있는 모델 — 스키마 vector(1536) 에 맞춘다
        body["dimensions"] = profile.embedding_dim
    for attempt in (1, 2):   # 일시 오류(5xx·429·연결 끊김)는 한 번 다시 시도한다
        try:
            r = httpx.post(f"{profile.base_url}/embeddings", timeout=60,
                           headers={"Authorization": f"Bearer {profile.api_key}"}, json=body)
            if attempt == 1 and (r.status_code >= 500 or r.status_code == 429):
                time.sleep(0.5)
                continue
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def embed_one(profile: LlmProfile, text: str) -> list[float] | None:
    v = embed(profile, [text])
    return v[0] if v else None
