"""질의 파이프라인: load_workspace → classify → (policy_rag ∥ graph_search) → gate → generate(스트리밍) → critic.
LangGraph StateGraph 로도 같은 노드를 배선하지만(build.py), SSE 스트리밍은 이 async generator 가 직접 구동한다
(개발문서 31 §3-6 폴백 채택: 그래프는 라우팅·검색, 생성은 그래프 밖에서 스트리밍). 이벤트 순서는 04 §4.3.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator

from app.config import ai_mode, profile as load_profile
from app.llm import client as llm
from app.llm.schemas import (ChainEdge, ChainEvent, ChainNode, CitationsEvent, DoneEvent, ErrorEvent, IntentEvent, Latency,
                             PolicyRef, StageEvent, TokenEvent)
from app.retrieval import db, embed
from app.retrieval.tokenize import Tokenizer, extract_error_code
from app.sse import sse

MARKER_RE = re.compile(r"\[([PN]\d+)\]")


async def run(workspace_id: int, policy_enabled: bool | None, query: str, session_id: int | None = None) -> AsyncIterator[bytes]:
    t0 = time.perf_counter()
    usage = llm.Usage()
    policy_ctx: list[dict] = []
    chain_ctx: list[dict] = []
    retrieved: list[str] = []
    try:
        # load_workspace (설정은 Spring 이 넘기지만, 라벨·프로필 등 settings 는 DB 에서 읽는다)
        with db.connect() as conn:
            ws = db.workspace_settings(conn, workspace_id)
            terms = db.workspace_terms(conn, workspace_id)
        if policy_enabled is None:
            policy_enabled = bool(ws["policy_enabled"])
        prof = load_profile(ws["settings"].get("llm_profile", "default"))
        mode = "policy" if policy_enabled else "dag_only"
        tokenizer = Tokenizer(terms)
        q_tokens = tokenizer.tokens(query)
        error_code = extract_error_code(query)

        # classify
        yield sse("stage", StageEvent(node="supervisor", status="start").model_dump())
        intent = (await llm.classify(prof, query, error_code, policy_enabled, usage)).intent
        yield sse("intent", IntentEvent(intent=intent, mode=mode).model_dump())
        yield sse("stage", StageEvent(node="supervisor", status="done").model_dump())

        # 임베딩은 한 번만 (openai 모드에서만 값이 있다)
        q_vec = await asyncio.to_thread(embed.embed_one, prof, query)

        # workers (병렬. 한쪽 실패는 stage error 로 보고하고 계속)
        do_policy = policy_enabled and intent in ("policy", "both")
        do_graph = intent in ("troubleshoot", "both") or not policy_enabled
        if do_policy:
            yield sse("stage", StageEvent(node="policy_rag", status="start").model_dump())
        if do_graph:
            yield sse("stage", StageEvent(node="graph_search", status="start").model_dump())
        results = await asyncio.gather(
            asyncio.to_thread(_policy_rag, workspace_id, q_tokens, q_vec) if do_policy else _noop(),
            asyncio.to_thread(_graph_search, workspace_id, q_tokens, q_vec, error_code) if do_graph else _noop(),
            return_exceptions=True)
        pol, gra = results
        if do_policy:
            if isinstance(pol, Exception):
                yield sse("stage", StageEvent(node="policy_rag", status="error", detail=str(pol)[:200]).model_dump())
            else:
                policy_ctx = pol
                yield sse("policy", [PolicyRef(**p).model_dump() for p in policy_ctx])
                yield sse("stage", StageEvent(node="policy_rag", status="done").model_dump())
                retrieved += [f"c:{p['chunk_id']}" for p in policy_ctx]
        if do_graph:
            if isinstance(gra, Exception):
                yield sse("stage", StageEvent(node="graph_search", status="error", detail=str(gra)[:200]).model_dump())
            else:
                chain_ctx, chain_event = gra
                if chain_event:
                    yield sse("chain", chain_event.model_dump())
                yield sse("stage", StageEvent(node="graph_search", status="done").model_dump())
                retrieved += [f"n:{n['id']}" for n in chain_ctx]
        retrieval_ms = int((time.perf_counter() - t0) * 1000)

        # gate
        if not policy_ctx and not chain_ctx:
            yield sse("stage", StageEvent(node="gate", status="done", detail="no_evidence").model_dump())
            msg = "근거 없음: 현재 워크스페이스에서 관련 규정이나 해결 사례를 찾지 못했습니다. 로그를 등록하거나 규정 문서를 추가해 주세요."
            yield sse("token", TokenEvent(text=msg).model_dump())
            yield sse("citations", CitationsEvent(verified=[], unverified=[]).model_dump())
            yield sse("done", _done(intent, retrieved, [], [], retrieval_ms, retrieval_ms, t0, "gate", usage).model_dump())
            return
        yield sse("stage", StageEvent(node="gate", status="done").model_dump())

        # generate (스트리밍)
        yield sse("stage", StageEvent(node="generate", status="start").model_dump())
        answer: list[str] = []
        first_token_ms: int | None = None
        async for piece in llm.generate(prof, query, policy_ctx, chain_ctx, usage):
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - t0) * 1000)
            answer.append(piece)
            yield sse("token", TokenEvent(text=piece).model_dump())
        yield sse("stage", StageEvent(node="generate", status="done").model_dump())

        # critic — LLM 미사용: 마커 ↔ 검색 집합 대조
        yield sse("stage", StageEvent(node="critic", status="start").model_dump())
        ref_to_id = {p["ref"]: f"c:{p['chunk_id']}" for p in policy_ctx} | {n["ref"]: f"n:{n['id']}" for n in chain_ctx}
        used = list(dict.fromkeys(MARKER_RE.findall("".join(answer))))
        verified = [m for m in used if m in ref_to_id]
        unverified = [m for m in used if m not in ref_to_id]
        yield sse("citations", CitationsEvent(verified=verified, unverified=unverified).model_dump())
        yield sse("stage", StageEvent(node="critic", status="done").model_dump())
        model = prof.model_answer if (ai_mode() == "openai" and prof.has_key) else "fake"
        yield sse("done", _done(intent, retrieved, [ref_to_id[m] for m in verified], unverified, retrieval_ms, first_token_ms or retrieval_ms, t0, model, usage).model_dump())
    except Exception as e:  # noqa: BLE001 — 전체 실패만 error 이벤트
        yield sse("error", ErrorEvent(message=str(e)[:300]).model_dump())


async def _noop():
    return None


def _policy_rag(ws: int, q_tokens: str, q_vec: list[float] | None) -> list[dict]:
    with db.connect() as conn:
        rows = db.search_chunks(conn, ws, q_tokens, q_vec, 5)
    return [{"ref": f"P{i + 1}", "chunk_id": r["id"], "doc_id": r["doc_id"], "doc_title": r["doc_title"], "version": r["version"],
             "article_label": r["article_label"], "page": r["page"], "text": r["text"]} for i, r in enumerate(rows)]


def _graph_search(ws: int, q_tokens: str, q_vec: list[float] | None, error_code: str | None) -> tuple[list[dict], ChainEvent | None]:
    with db.connect() as conn:
        hits = db.search_nodes(conn, ws, q_tokens, q_vec, error_code, 1)
        if not hits:
            return [], None
        entry = hits[0]["node_id"]
        # 루트 기준 체인: 진입 노드의 루트에서 하향 (BE-HANDOFF-001 §4 ④)
        root = db.root_of(conn, ws, entry) or entry
        ch = db.chain(conn, ws, root, 4, 30)
    nodes = sorted(ch["nodes"], key=lambda n: (n["depth"], n["id"]))  # 마커 번호 = depth 안정 정렬 순서
    ctx, ev_nodes = [], []
    for i, n in enumerate(nodes):
        ref = f"N{i + 1}"
        ctx.append({"ref": ref, "id": n["id"], "type": n["type"], "status": n["status"], "title": n["title"],
                    "resolution_summary": n.get("resolution_summary"), "depth": n["depth"]})
        ev_nodes.append(ChainNode(ref=ref, id=n["id"], type=n["type"], status=n["status"], title=n["title"], depth=n["depth"],
                                  path_label=n.get("path_label"),
                                  last_confirmed_at=n["last_confirmed_at"].isoformat() if n.get("last_confirmed_at") else None))
    return ctx, ChainEvent(entry_id=entry, nodes=ev_nodes, edges=[ChainEdge(**e) for e in ch["edges"]])


def _done(intent, retrieved, verified_ids, unverified_refs, retrieval_ms, first_token_ms, t0, model, usage) -> DoneEvent:
    return DoneEvent(intent=intent, retrieved_ids=retrieved, verified_ids=verified_ids, unverified_ids=unverified_refs,
                     latency=Latency(retrieval_ms=retrieval_ms, first_token_ms=first_token_ms, total_ms=int((time.perf_counter() - t0) * 1000)),
                     model=model, prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)
