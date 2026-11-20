"""임베딩. OpenAI 호환 /embeddings 를 httpx 로 호출한다. fake 모드(키 없음)에서는 None 을 돌려 어휘 검색만으로 동작한다."""
from __future__ import annotations

import time

import httpx

from app.config import LlmProfile, ai_mode, api_key_of, key_rejected, switch_to_backup


def embed(profile: LlmProfile, texts: list[str]) -> list[list[float]] | None:
    if ai_mode() != "openai" or not profile.has_key or not texts:
        return None
    body: dict = {"model": profile.embedding_model, "input": texts}
    if profile.embedding_model.startswith("text-embedding-3"):   # 차원을 줄여 받을 수 있는 모델 — 스키마 vector(1536) 에 맞춘다
        body["dimensions"] = profile.embedding_dim
    for attempt in (1, 2):   # 일시 오류(5xx·429·연결 끊김)는 한 번 다시 시도한다. 키가 막힌 것이면 예비 키로 다시 보낸다
        try:
            r = httpx.post(f"{profile.base_url}/embeddings", timeout=60,
                           headers={"Authorization": f"Bearer {api_key_of(profile)}"}, json=body)
            if key_rejected(r.status_code, r.text) and switch_to_backup(profile):
                r = httpx.post(f"{profile.base_url}/embeddings", timeout=60,
                               headers={"Authorization": f"Bearer {api_key_of(profile)}"}, json=body)
            if attempt == 1 and (r.status_code >= 500 or r.status_code == 429) and not key_rejected(r.status_code, r.text):
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


EMBED_CHARS = 6000   # 한 건의 입력을 먼저 이 길이로 자른다. 모델의 한도는 토큰 수라(한글은 한 자가 1~2토큰) 이 길이로도 넘칠 수 있다


def embed_one(profile: LlmProfile, text: str) -> list[float] | None:
    """글 하나의 벡터. 입력 한도를 넘어 400 이 오면 앞쪽 절반으로 줄여 다시 보낸다 — 긴 글도 앞부분의 뜻으로 검색되게."""
    text = text[:EMBED_CHARS]
    while True:
        try:
            v = embed(profile, [text])
            return v[0] if v else None
        except httpx.HTTPStatusError as e:
            if getattr(e.response, "status_code", None) != 400 or len(text) <= EMBED_CHARS // 8:
                raise
            text = text[:len(text) // 2]
