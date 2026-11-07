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
    source: Literal["search", "case"] = "search"   # case = 검색이 아니라 체인의 사례가 따른 규정이라 실린 것


class NodePolicy(BaseModel):
    """사례 노드의 근거 조문. follows = 규정을 따른 조치, exception = 규정과 달리 한 조치, gap = 규정에 기준이 없음."""
    relation: Literal["follows", "exception", "gap"]
    ref: str | None = None      # policy 이벤트의 마커(P1…). gap 과 revision=replaced 는 없음
    label: str | None = None    # '배포·장애 대응 운영지침 제12조'
    note: str | None = None
    topic: str | None = None    # gap: 규정에 없는 기준
    # 규정이 개정된 사례: amended = 그 조문의 내용이 바뀜(ref 는 지금 조문), replaced = 개정판에 대응 조문이 정해지지 않음(ref 없음, label 은 개정 전 조문)
    revision: Literal["amended", "replaced"] | None = None
    from_version: str | None = None   # 사례가 따른 판


class ChainNode(BaseModel):
    ref: str
    id: int
    type: Literal["fail", "success", "ref"]
    status: Literal["active", "deprecated"]
    title: str
    depth: int
    path_label: str | None = None
    last_confirmed_at: str | None = None
    policy: list[NodePolicy] | None = None
    resolves_root: bool = False   # 이 success 가 장애 건을 실제로 해결한 기록으로 남아 있다
    is_cause: bool = False        # 장애 건의 원인으로 표시한 기록(상위가 있을 때만)


class ChainEdge(BaseModel):
    parent_id: int
    child_id: int
    reason: str | None = None     # 이 기록을 그 상위에 이어 붙인 이유


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
