"""질의 파이프라인: load_workspace → classify → (policy_rag ∥ graph_search) → gate → generate(스트리밍) → critic.
LangGraph StateGraph 로도 같은 노드를 배선하지만(build.py), SSE 스트리밍은 이 async generator 가 직접 구동한다
(개발문서 31 §3-6 폴백 채택: 그래프는 라우팅·검색, 생성은 그래프 밖에서 스트리밍). 이벤트 순서는 04 §4.3.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator, NamedTuple

from app.config import ai_mode, profile as load_profile
from app.llm import client as llm
from app.llm.schemas import (ChainEdge, ChainEvent, ChainNode, CitationsEvent, DoneEvent, ErrorEvent, IntentEvent, Latency,
                             PolicyRef, StageEvent, TokenEvent)
from app.retrieval import db, embed
from app.retrieval.tokenize import Tokenizer, extract_error_code, synonym_groups
from app.sse import sse

MARKER_RE = re.compile(r"\[([PN]\d+)\]")
TRUNCATED_NOTE = "\n(답변 길이 상한에 도달해 이하 생략)"


async def run(workspace_id: int, policy_enabled: bool | None, query: str, session_id: int | None = None) -> AsyncIterator[bytes]:
    t0 = time.perf_counter()
    usage = llm.Usage()
    policy_ctx: list[dict] = []
    chain_ctx: list[dict] = []
    retrieved: list[str] = []
    try:
        # load_workspace (설정은 Spring 이 넘기지만, 라벨·프로필 등 settings 는 DB 에서 읽는다)
        error_code = extract_error_code(query)
        with db.connect() as conn:
            ws = db.workspace_settings(conn, workspace_id)
            terms = db.user_terms(conn)
            cctx = db.classify_context(conn, workspace_id, error_code)
        if policy_enabled is None:
            policy_enabled = bool(ws["policy_enabled"])
        prof = load_profile(ws["settings"].get("llm_profile", "default"))
        mode = "policy" if policy_enabled else "dag_only"
        tokenizer = Tokenizer(terms)
        q_tokens = tokenizer.query_tokens(query, synonym_groups(ws["settings"]))

        # 질의 임베딩은 분류와 병렬로 시작 (openai 모드에서만 값이 있다. 분류가 규칙으로 즉시 끝나면 임베딩이 임계 경로)
        q_vec_task = asyncio.create_task(asyncio.to_thread(embed.embed_one, prof, query))

        # classify — 규칙으로 결정되면 LLM 호출 없음
        yield sse("stage", StageEvent(node="supervisor", status="start").model_dump())
        intent = (await llm.classify(prof, query, error_code, policy_enabled, usage, **cctx)).intent
        yield sse("intent", IntentEvent(intent=intent, mode=mode).model_dump())
        yield sse("stage", StageEvent(node="supervisor", status="done").model_dump())

        q_vec = await q_vec_task

        # workers (병렬. 한쪽 실패는 stage error 로 보고하고 계속)
        do_policy = policy_enabled and intent in ("policy", "both")
        do_graph = intent in ("troubleshoot", "both") or not policy_enabled
        if do_policy:
            yield sse("stage", StageEvent(node="policy_rag", status="start").model_dump())
        if do_graph:
            yield sse("stage", StageEvent(node="graph_search", status="start").model_dump())
        results = await asyncio.gather(
            asyncio.to_thread(_policy_rag, workspace_id, q_tokens, q_vec) if do_policy else _noop(),
            _graph_search(workspace_id, q_tokens, q_vec, error_code, CASE_FLOOR.get(prof.embedding_model), prof, query, usage) if do_graph else _noop(),
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
                yield sse("stage", StageEvent(node="graph_search", status="done", detail=None if chain_ctx else "no_relevant_case").model_dump())
                retrieved += [f"n:{n['id']}" for n in chain_ctx]
        # 사례만 찾기로 했는데 관련 사례가 없으면 규정에서 근거를 찾는다
        if do_graph and not do_policy and policy_enabled and not chain_ctx:
            yield sse("stage", StageEvent(node="policy_rag", status="start", detail="fallback").model_dump())
            try:
                policy_ctx = await asyncio.to_thread(_policy_rag, workspace_id, q_tokens, q_vec)
                yield sse("policy", [PolicyRef(**p).model_dump() for p in policy_ctx])
                yield sse("stage", StageEvent(node="policy_rag", status="done").model_dump())
                retrieved += [f"c:{p['chunk_id']}" for p in policy_ctx]
            except Exception as e:  # noqa: BLE001 — 폴백 실패는 '근거 없음'으로 이어진다
                yield sse("stage", StageEvent(node="policy_rag", status="error", detail=str(e)[:200]).model_dump())
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
        truncated = usage.finish_reason == "length"
        if truncated:
            yield sse("token", TokenEvent(text=TRUNCATED_NOTE).model_dump())
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
        yield sse("done", _done(intent, retrieved, [ref_to_id[m] for m in verified], unverified, retrieval_ms, first_token_ms or retrieval_ms, t0, model, usage,
                                truncated).model_dump())
    except Exception as e:  # noqa: BLE001 — 전체 실패만 error 이벤트
        yield sse("error", ErrorEvent(message=str(e)[:300]).model_dump())


async def _noop():
    return None


COMPANY_SLOTS = 2   # 근거 5건 중 회사 규정에 보장하는 자리 (후보 20건 안에 있을 때)


def _with_company_rules(rows: list[dict], k: int = 5, slots: int = COMPANY_SLOTS) -> list[dict]:
    """외부 기준(고시)은 분량이 많아 같은 주제의 회사 규정을 밀어낸다. 상위 k 에 회사 규정이 모자라면 가장 낮은 순위의 고시와 바꾼다."""
    top, rest = rows[:k], rows[k:]
    pool = [r for r in rest if r["doc_type"] != "고시"]
    need = slots - sum(r["doc_type"] != "고시" for r in top)
    while need > 0 and pool:
        ext = [r for r in top if r["doc_type"] == "고시"]
        if not ext:
            break
        top.remove(ext[-1])
        top.append(pool.pop(0))
        need -= 1
    return top


def _policy_rag(ws: int, q_tokens: str, q_vec: list[float] | None) -> list[dict]:
    with db.connect() as conn:
        rows = _with_company_rules(db.search_chunks(conn, ws, q_tokens, q_vec, 20))
    return [{"ref": f"P{i + 1}", "chunk_id": r["id"], "doc_id": r["doc_id"], "doc_title": r["doc_title"], "version": r["version"],
             "article_label": r["article_label"], "page": r["page"], "text": r["text"]} for i, r in enumerate(rows)]


class CaseFloor(NamedTuple):
    """사례 관련성 기준. near: 이 거리 안이면 후보, far·terms: 조금 멀어도 질의 단어가 이만큼 일치하면 후보, sure: 이 거리 안이면 판정 없이 채택."""
    near: float
    far: float
    terms: int
    sure: float


# 벡터 거리 분포는 임베딩 모델마다 달라 보정한 모델에만 적용한다
CASE_FLOOR = {"text-embedding-3-small": CaseFloor(near=0.62, far=0.70, terms=2, sure=0.45)}
JUDGE_TIMEOUT = 4.0


def _relevant(hit: dict, floor: CaseFloor | None) -> bool:
    """에러 코드 일치, 벡터 거리가 가까움, 조금 멀어도 질의 단어가 여럿 일치 — 셋 중 하나면 후보로 둔다. 벡터가 없으면 판단하지 않는다."""
    if floor is None or hit["exact"] or hit["distance"] is None:
        return True
    return hit["distance"] <= floor.near or (hit["distance"] <= floor.far and hit["lex_terms"] >= floor.terms)


def _sure(hit: dict, floor: CaseFloor | None) -> bool:
    """판정 없이 채택하는 후보: 에러 코드 일치이거나 아주 가깝다."""
    return floor is None or hit["exact"] or hit["distance"] is None or hit["distance"] <= floor.sure


def _case_candidates(ws: int, q_tokens: str, q_vec: list[float] | None, error_code: str | None, floor: CaseFloor | None) -> list[dict]:
    with db.connect() as conn:
        hits = [h for h in db.search_case_entry(conn, ws, q_tokens, q_vec, error_code, 3) if _relevant(h, floor)]
        if hits and not _sure(hits[0], floor):   # 판정에 보여 줄 제목·요약
            briefs = db.case_briefs(conn, ws, [h["node_id"] for h in hits])
            hits = [{**h, **briefs[h["node_id"]]} for h in hits if h["node_id"] in briefs]
    return hits


async def _choose_entry(prof, query: str, hits: list[dict], floor: CaseFloor | None, usage) -> int | None:
    """후보 중 진입 노드. 경계선 후보(주제만 비슷할 수 있다)는 가벼운 모델이 후보마다 같은 문제인지 보고(병렬), 통과한 첫 후보를 쓴다.
    1위 후보의 판정이 실패하면 순위를 그대로 믿는다."""
    if not hits:
        return None
    if _sure(hits[0], floor):
        return hits[0]["node_id"]

    async def judge(h: dict) -> bool | None:
        try:
            return await asyncio.wait_for(llm.judge_case(prof, query, h, usage), JUDGE_TIMEOUT)
        except Exception:  # noqa: BLE001
            return None

    verdicts = await asyncio.gather(*(judge(h) for h in hits))
    if verdicts[0] is None:
        return hits[0]["node_id"]
    return next((h["node_id"] for h, v in zip(hits, verdicts) if v), None)


async def _graph_search(ws: int, q_tokens: str, q_vec: list[float] | None, error_code: str | None,
                        floor: CaseFloor | None = None, prof=None, query: str = "", usage=None) -> tuple[list[dict], ChainEvent | None]:
    hits = await asyncio.to_thread(_case_candidates, ws, q_tokens, q_vec, error_code, floor)
    entry = await _choose_entry(prof, query, hits, floor, usage)
    if entry is None:
        return [], None
    return await asyncio.to_thread(_load_chain, ws, entry)


def _load_chain(ws: int, entry: int) -> tuple[list[dict], ChainEvent]:
    with db.connect() as conn:
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


def _done(intent, retrieved, verified_ids, unverified_refs, retrieval_ms, first_token_ms, t0, model, usage, truncated=False) -> DoneEvent:
    return DoneEvent(intent=intent, retrieved_ids=retrieved, verified_ids=verified_ids, unverified_ids=unverified_refs,
                     latency=Latency(retrieval_ms=retrieval_ms, first_token_ms=first_token_ms, total_ms=int((time.perf_counter() - t0) * 1000)),
                     model=model, prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens, truncated=truncated)
