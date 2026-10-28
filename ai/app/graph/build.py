"""LangGraph StateGraph 배선 (발표·테스트용 구조 명세). 실행 스트리밍은 pipeline.run 이 담당하고,
이 그래프는 `langgraph` 가 설치된 환경에서 노드·간선 구조를 검증하거나 비스트리밍 실행에 쓴다.
노드: load_workspace → classify_intent → (policy_rag ∥ graph_search) → context_gate → generate → critic
"""
from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    workspace_id: int
    policy_enabled: bool
    query: str
    intent: str
    policy_ctx: list[dict]
    chain_ctx: list[dict]
    answer: str
    verified: list[str]
    unverified: list[str]


def route_workers(state: GraphState) -> list[str]:
    if not state.get("policy_enabled"):
        return ["graph_search"]
    return {"policy": ["policy_rag"], "troubleshoot": ["graph_search"], "both": ["policy_rag", "graph_search"]}[state.get("intent", "both")]


def build() -> Any:
    from langgraph.graph import END, START, StateGraph  # type: ignore

    g = StateGraph(GraphState)
    g.add_node("load_workspace", lambda s: s)
    g.add_node("classify_intent", lambda s: {"intent": s.get("intent", "both")})
    g.add_node("policy_rag", lambda s: {"policy_ctx": s.get("policy_ctx", [])})
    g.add_node("graph_search", lambda s: {"chain_ctx": s.get("chain_ctx", [])})
    g.add_node("context_gate", lambda s: s)
    g.add_node("generate", lambda s: {"answer": s.get("answer", "")})
    g.add_node("critic", lambda s: {"verified": s.get("verified", []), "unverified": s.get("unverified", [])})
    g.add_edge(START, "load_workspace")
    g.add_edge("load_workspace", "classify_intent")
    g.add_conditional_edges("classify_intent", route_workers, ["policy_rag", "graph_search"])
    g.add_edge("policy_rag", "context_gate")
    g.add_edge("graph_search", "context_gate")
    g.add_edge("context_gate", "generate")
    g.add_edge("generate", "critic")
    g.add_edge("critic", END)
    return g.compile()
