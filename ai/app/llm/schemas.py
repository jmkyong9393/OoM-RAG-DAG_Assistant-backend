"""구조화 출력 스키마 + SSE 이벤트 payload 원본(Pydantic). 프론트 타입은 여기서 생성한다 (scripts/gen_sse_types.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["policy", "troubleshoot", "both"]
Mode = Literal["policy", "dag_only"]
StageNode = Literal["supervisor", "policy_rag", "graph_search", "gate", "generate", "critic"]


class IntentResult(BaseModel):
    intent: Intent
    reason: str = ""


class RefineResult(BaseModel):
    error_code: str | None = None
    keywords: list[str] = Field(default_factory=list)
    resolution_summary: str | None = None


# ── SSE payload ──────────────────────────────────────────────────────────────

class StageEvent(BaseModel):
    node: StageNode
    status: Literal["start", "done", "error"]
    detail: str | None = None


class IntentEvent(BaseModel):
    intent: Intent
    mode: Mode


class PolicyRef(BaseModel):
    ref: str
    chunk_id: str
    doc_id: int
    doc_title: str
    version: str
    article_label: str
    page: int | None = None
    text: str


class ChainNode(BaseModel):
    ref: str
    id: int
    type: Literal["fail", "success", "ref"]
    status: Literal["active", "deprecated"]
    title: str
    depth: int
    path_label: str | None = None
    last_confirmed_at: str | None = None


class ChainEdge(BaseModel):
    parent_id: int
    child_id: int


class ChainEvent(BaseModel):
    entry_id: int
    nodes: list[ChainNode]
    edges: list[ChainEdge]


class TokenEvent(BaseModel):
    text: str


class CitationsEvent(BaseModel):
    verified: list[str]
    unverified: list[str]


class Latency(BaseModel):
    retrieval_ms: int
    first_token_ms: int
    total_ms: int


class DoneEvent(BaseModel):
    intent: Intent
    retrieved_ids: list[str]
    verified_ids: list[str]
    unverified_ids: list[str]
    latency: Latency
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False   # 답변이 ANSWER_MAX_TOKENS 상한에 걸려 잘렸음


class ErrorEvent(BaseModel):
    message: str
    node: StageNode | None = None
