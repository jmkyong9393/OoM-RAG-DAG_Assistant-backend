"""LangGraph 배선 구조 테스트 (langgraph 설치 시)."""
import pytest

from app.graph.build import route_workers


def test_route_workers():
    assert route_workers({"policy_enabled": False, "intent": "both"}) == ["graph_search"]
    assert route_workers({"policy_enabled": True, "intent": "policy"}) == ["policy_rag"]
    assert route_workers({"policy_enabled": True, "intent": "troubleshoot"}) == ["graph_search"]
    assert route_workers({"policy_enabled": True, "intent": "both"}) == ["policy_rag", "graph_search"]


def test_graph_compiles_and_runs_fanout():
    pytest.importorskip("langgraph")
    from app.graph.build import build
    g = build()
    nodes = set(g.get_graph().nodes)
    for n in ("load_workspace", "classify_intent", "policy_rag", "graph_search", "context_gate", "generate", "critic"):
        assert n in nodes
    out = g.invoke({"workspace_id": 2, "policy_enabled": True, "intent": "both", "query": "q"})
    assert "policy_ctx" in out and "chain_ctx" in out and "verified" in out
