"""질의 파이프라인: load_workspace → classify → (policy_rag ∥ graph_search) → gate → generate(스트리밍) → critic.
LangGraph StateGraph 로도 같은 노드를 배선하지만(build.py), SSE 스트리밍은 이 async generator 가 직접 구동한다
(개발문서 31 §3-6 폴백 채택: 그래프는 라우팅·검색, 생성은 그래프 밖에서 스트리밍). 이벤트 순서는 04 §4.3.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import AsyncIterator, NamedTuple

from app.config import ai_mode, policy_rerank, profile as load_profile
from app.llm import client as llm
from app.llm.schemas import (ChainEdge, ChainEvent, ChainNode, NodePolicy, CitationsEvent, DoneEvent, ErrorEvent, IntentEvent, Latency,
                             PolicyRef, StageEvent, TokenEvent)
from app.retrieval import db, embed, rerank
from app.retrieval.tokenize import Tokenizer, extract_error_code, synonym_groups
from app.sse import sse

MARKER_RE = re.compile(r"\[((?:[PN]\d+)(?:\s*,\s*[PN]\d+)*)\]")   # [P1] 과, 모델이 묶어 쓴 [P1, P2] 도 읽는다


def markers_in(text: str) -> list[str]:
    """답변에 쓰인 마커를 나온 순서대로(중복 없이). 한 괄호에 묶인 것은 풀어서."""
    return list(dict.fromkeys(m.strip() for g in MARKER_RE.findall(text) for m in g.split(",")))
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
            stale_vec = _stale_vectors(conn, ws)
            scope_docs = db.scope_documents(conn, workspace_id)
        if policy_enabled is None:
            policy_enabled = bool(ws["policy_enabled"])
        # 부서 설정: 사례를 끈 부서(규정 참조형)는 사례를 찾지 않는다. 규정까지 꺼져 있으면 사례가 유일한 근거라 켠 것으로 본다
        case_enabled = bool(ws["case_enabled"]) or not policy_enabled
        cctx["has_cases"] = cctx["has_cases"] and case_enabled
        prof = load_profile(ws["settings"].get("llm_profile", "default"))
        mode = "policy" if policy_enabled else "dag_only"
        tokenizer = Tokenizer(terms)
        q_tokens = tokenizer.query_tokens(query, synonym_groups(ws["settings"]))

        # 질의 임베딩은 분류와 병렬로 시작 (openai 모드에서만 값이 있다. 분류가 규칙으로 즉시 끝나면 임베딩이 임계 경로)
        q_vec_task = asyncio.create_task(_noop() if stale_vec else asyncio.to_thread(embed.embed_one, prof, query))

        # classify — 규칙으로 결정되면 LLM 호출 없음
        yield sse("stage", StageEvent(node="supervisor", status="start").model_dump())
        intent = (await llm.classify(prof, query, error_code, policy_enabled, usage, **cctx)).intent
        yield sse("intent", IntentEvent(intent=intent, mode=mode).model_dump())
        yield sse("stage", StageEvent(node="supervisor", status="done").model_dump())

        try:
            q_vec = await q_vec_task
        except Exception as e:  # noqa: BLE001 — 임베딩이 실패해도 어휘 검색으로 답한다
            print(f"[pipeline] 질의 임베딩 실패, 어휘 검색만 쓴다: {type(e).__name__}", flush=True)
            q_vec = None

        # workers (병렬. 한쪽 실패는 stage error 로 보고하고 계속)
        do_policy = policy_enabled and intent in ("policy", "both")
        floor = CASE_FLOOR.get(prof.embedding_model)
        # 규정 질의에도 사례를 찾는다: 사례가 있는 부서(규정+사례형)에서는 "영수증 없이 환불 되나요" 같은 규정 모양의 질문에 처리 사례가 답의 절반이다.
        # 관련성을 판정할 수 있을 때만(보정된 임베딩 + 벡터) — 판정 없이 붙이면 규정 답변마다 엉뚱한 체인이 따라온다
        with_cases = cctx["has_cases"] and floor is not None and q_vec is not None
        do_graph = case_enabled and (intent in ("troubleshoot", "both") or not policy_enabled or with_cases)
        if do_policy:
            yield sse("stage", StageEvent(node="policy_rag", status="start").model_dump())
        if do_graph:
            yield sse("stage", StageEvent(node="graph_search", status="start").model_dump())
        if do_policy and do_graph:
            # 규정 후보를 먼저 찾아, 상위 조문을 근거로 적어 둔 사례를 사례 후보에 더한다 (규정 검색은 수십 ms, 사례 쪽은 판정이 더 길다).
            # 재순위는 검색 상위 2건을 최종 근거에 남기므로 그 2건으로 사례 쪽을 바로 출발시키고, 재순위와 사례 판정을 함께 돈다
            try:
                rows = await asyncio.to_thread(_policy_candidates, workspace_id, q_tokens, q_vec)
                linked_to = _article_keys(_with_company_rules(rows)[:LINK_TOP])
            except Exception as e:  # noqa: BLE001
                rows, linked_to = e, []
            pol, gra = await asyncio.gather(
                _policy_rank(prof, query, rows, usage) if not isinstance(rows, Exception) else _raise(rows),
                _graph_search(workspace_id, q_tokens, q_vec, error_code, floor, prof, query, usage, linked_to),
                return_exceptions=True)
        else:
            pol, gra = await asyncio.gather(
                _policy_rag(workspace_id, q_tokens, q_vec, prof, query, usage) if do_policy else _noop(),
                _graph_search(workspace_id, q_tokens, q_vec, error_code, floor, prof, query, usage) if do_graph else _noop(),
                return_exceptions=True)
        chain_event = None
        if do_policy and not isinstance(pol, Exception):
            policy_ctx = pol
        if do_graph and not isinstance(gra, Exception):
            chain_ctx, chain_event = gra
        # 체인의 사례가 따른(또는 벗어난) 규정 조문을 근거에 더한다. 실패해도 답변은 계속한다
        linked = 0
        if chain_ctx and policy_enabled:
            try:
                linked = await asyncio.to_thread(_link_policies, workspace_id, chain_ctx, chain_event, policy_ctx)
            except Exception:  # noqa: BLE001
                linked = 0
        if do_policy and isinstance(pol, Exception):
            yield sse("stage", StageEvent(node="policy_rag", status="error", detail=str(pol)[:200]).model_dump())
        if policy_ctx:
            if not do_policy:
                yield sse("stage", StageEvent(node="policy_rag", status="start", detail="linked").model_dump())
            yield sse("policy", [PolicyRef(**p).model_dump() for p in policy_ctx])
            yield sse("stage", StageEvent(node="policy_rag", status="done").model_dump())
            retrieved += [f"c:{p['chunk_id']}" for p in policy_ctx]
        elif do_policy and not isinstance(pol, Exception):
            yield sse("policy", [])
            yield sse("stage", StageEvent(node="policy_rag", status="done").model_dump())
        if do_graph:
            if isinstance(gra, Exception):
                yield sse("stage", StageEvent(node="graph_search", status="error", detail=str(gra)[:200]).model_dump())
            else:
                if chain_event:
                    yield sse("chain", chain_event.model_dump())
                yield sse("stage", StageEvent(node="graph_search", status="done", detail=None if chain_ctx else "no_relevant_case").model_dump())
                retrieved += [f"n:{n['id']}" for n in chain_ctx]
        # 사례만 찾기로 했는데 관련 사례가 없으면 규정에서 근거를 찾는다
        if do_graph and not do_policy and policy_enabled and not chain_ctx:
            yield sse("stage", StageEvent(node="policy_rag", status="start", detail="fallback").model_dump())
            try:
                policy_ctx = await _policy_rag(workspace_id, q_tokens, q_vec, prof, query, usage)
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
        async for piece in llm.generate(prof, query, policy_ctx, chain_ctx, usage, case_required=intent != "policy"):
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - t0) * 1000)
            answer.append(piece)
            yield sse("token", TokenEvent(text=piece).model_dump())
        truncated = usage.finish_reason == "length"
        if truncated:
            yield sse("token", TokenEvent(text=TRUNCATED_NOTE).model_dump())
        # 근거에 없는 다른 규정 영역 안내 — 모델이 아니라 서버가 고정 문구로(같은 부서 범위에 실제 있는 문서 제목만)
        notice = llm.other_rule_notice(query, policy_ctx, scope_docs, "".join(answer)) if policy_enabled else None
        if notice:
            answer.append("\n\n" + notice)
            yield sse("token", TokenEvent(text="\n\n" + notice).model_dump())
        yield sse("stage", StageEvent(node="generate", status="done").model_dump())

        # critic — LLM 미사용: 마커 ↔ 검색 집합 대조
        yield sse("stage", StageEvent(node="critic", status="start").model_dump())
        ref_to_id = {p["ref"]: f"c:{p['chunk_id']}" for p in policy_ctx} | {n["ref"]: f"n:{n['id']}" for n in chain_ctx}
        used = markers_in("".join(answer))
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


_warned_stale = False


def _stale_vectors(conn, ws: dict) -> bool:
    """저장된 벡터가 지금 임베딩 모델과 다른 모델로 만들어졌으면 벡터 검색을 쓰지 않는다(어휘 검색만). 재색인 전까지의 방어."""
    global _warned_stale
    if ai_mode() != "openai":
        return False
    model = load_profile(ws["settings"].get("llm_profile", "default")).embedding_model
    stale = db.embedding_mismatch(conn, model)
    if stale and not _warned_stale:
        _warned_stale = True
        print(f"[pipeline] 저장된 벡터가 {model} 로 만든 것이 아니다 — 어휘 검색만 쓴다. `python -m app.worker --reindex` 로 다시 만든다", flush=True)
    return stale


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


def _policy_candidates(ws: int, q_tokens: str, q_vec: list[float] | None) -> list[dict]:
    with db.connect() as conn:
        return db.search_chunks(conn, ws, q_tokens, q_vec, rerank.POOL)


async def _policy_rank(prof, query: str, rows: list[dict], usage: llm.Usage) -> list[dict]:
    """후보 → (재순위) → 상위 5(회사 규정 자리 보장) → 근거 항목."""
    if policy_rerank():
        rows = await rerank.rerank(prof, query, rows, usage)
    return [_policy_entry(i + 1, r, "search") for i, r in enumerate(_with_company_rules(rows))]


async def _policy_rag(ws: int, q_tokens: str, q_vec: list[float] | None, prof, query: str, usage: llm.Usage) -> list[dict]:
    return await _policy_rank(prof, query, await asyncio.to_thread(_policy_candidates, ws, q_tokens, q_vec), usage)


async def _raise(e: Exception):
    raise e


def _policy_entry(n: int, r: dict, source: str) -> dict:
    return {"ref": f"P{n}", "chunk_id": r["id"], "doc_id": r["doc_id"], "doc_title": r["doc_title"], "version": r["version"],
            "article_label": r["article_label"], "page": r["page"], "text": r["text"], "slug": r["slug"], "source": source}


LINK_TOP = 2      # 규정 → 사례: 상위 몇 개 조문에 연결된 사례를 후보로 볼 것인가
MAX_LINKED = 3    # 사례 → 규정: 체인이 가리키는 조문 중 근거에 더할 최대 수
ARTICLE_RE = re.compile(r"\[?(제\d+조(?:의\d+)?|별표 ?\d+|Q\d+)")   # 조문 · 별표 · FAQ 항목


def _article_keys(policies: list[dict]) -> list[tuple[str, str]]:
    """검색된 규정 청크 → (문서 slug, 조문). 대조표가 쓰는 표기와 같다."""
    out = []
    for p in policies:
        m = ARTICLE_RE.match(p["article_label"] or "")
        if m and (p["slug"], m.group(1)) not in out:
            out.append((p["slug"], m.group(1)))
    return out


def _link_policies(ws: int, chain_ctx: list[dict], chain_event: ChainEvent | None, policy_ctx: list[dict]) -> int:
    """체인 노드의 근거 조문(attributes.policy_refs)을 근거 목록에 더하고, 노드마다 어느 근거인지 붙인다.
    규정 검색 결과가 이미 있으면 새로 더하는 조문은 진입 노드·체인 루트·예외 사례·진입 노드와 같은 줄기의 해결 노드가 가리키는 것만 — 질문과 먼 조문이
    섞이지 않게. 자리(MAX_LINKED)는 진입 노드, 같은 줄기, 예외, 해결 기록, 해결 순으로 준다.
    검색 결과가 없으면(사례만 찾은 질의) 체인의 조문을 진입 노드·예외·해결 노드 순으로 MAX_LINKED 개까지 싣는다.
    그 밖의 노드는 이미 실린 근거와 일치할 때만 표시한다. policy_ctx·chain_ctx·chain_event 를 고쳐 쓴다. 더한 근거 수를 돌려준다."""
    entry = chain_event.entry_id if chain_event else None
    pairs = [(n, r) for n in chain_ctx if n["status"] == "active" for r in (n.get("policy_refs") or []) if isinstance(r, dict)]
    if not pairs:
        return 0
    keys = list(dict.fromkeys((r["policy"], r["article"]) for _, r in pairs if r.get("policy") and r.get("article")))
    with db.connect() as conn:
        chunks = db.policy_ref_chunks(conn, ws, keys)
    by_chunk = {p["chunk_id"]: p for p in policy_ctx}
    # 체인이 가리키는 규정이 개정됐는가 — '기준 없음'은 문서를 적어 두지 않으므로, 같은 체인의 다른 근거로 판단한다
    revised = any(c.get("status") in ("amended", "replaced", "retired") for c in chunks.values())
    wide = not policy_ctx
    added = 0
    # 자리 순서: 진입 → 같은 줄기 → 예외 → 장애 건을 해결한 기록 → 그 밖의 해결 → 깊이. 해결 기록을 등록 순서에 기대지 않고 앞에 둔다
    for n, r in sorted(pairs, key=lambda x: (x[0]["id"] != entry, not x[0].get("near"), x[1].get("relation") != "exception",
                                             not x[0].get("resolves_root"), x[0]["type"] != "success", x[0]["depth"])):
        if r.get("relation") == "gap":
            n.setdefault("policy", []).append({"relation": "gap", "topic": r.get("topic"), "note": r.get("note"),
                                               **({"revision": "replaced"} if revised else {})})   # 당시의 판단 — 개정판에는 기준이 생겼을 수 있다
            continue
        row = chunks.get((r.get("policy"), r.get("article")))
        if not row or r.get("relation") not in ("follows", "exception"):
            continue
        if row.get("status") in ("replaced", "retired"):
            # 규정이 개정돼 대응 조문이 정해지지 않았다 — 폐기된 조문을 근거로 싣지 않고, 개정 전 기준이었다는 사실만 붙인다
            n.setdefault("policy", []).append({"relation": r["relation"], "ref": None, "label": f"{row['from_title']} {row['from_version']} {r['article']}",
                                               "note": r.get("note"), "revision": "replaced", "from_version": row["from_version"]})
            continue
        amended = row.get("status") == "amended"
        p = by_chunk.get(row["id"])
        if p is None:
            key_node = (wide or n["id"] == entry or n["depth"] == 0 or r["relation"] == "exception" or amended
                        or (n.get("near") and n["type"] == "success"))   # 답변이 인용할 해결 줄의 근거 — 지시(_must_include)와 맞춘다
            if not key_node or added >= MAX_LINKED:
                continue
            p = _policy_entry(len(policy_ctx) + 1, row, "case")
            policy_ctx.append(p)
            by_chunk[row["id"]] = p
            added += 1
        n.setdefault("policy", []).append({"relation": r["relation"], "ref": p["ref"], "label": f"{p['doc_title']} {r['article']}", "note": r.get("note"),
                                           **({"revision": "amended", "from_version": row["from_version"]} if amended else {})})
    if chain_event:
        marks = {n["id"]: n["policy"] for n in chain_ctx if n.get("policy")}
        for ev in chain_event.nodes:
            if ev.id in marks:
                ev.policy = [NodePolicy(**m) for m in marks[ev.id]]
    return added


class CaseFloor(NamedTuple):
    """사례 관련성 기준. near: 이 거리 안이면 후보, far·terms: 조금 멀어도 질의 단어가 이만큼 일치하면 후보, sure: 이 거리 안이면 판정 없이 채택."""
    near: float
    far: float
    terms: int
    sure: float


# 벡터 거리 분포는 임베딩 모델마다 달라 보정한 모델에만 적용한다
CASE_FLOOR = {"text-embedding-3-small": CaseFloor(near=0.62, far=0.70, terms=2, sure=0.45),
              "text-embedding-3-large": CaseFloor(near=0.62, far=0.70, terms=2, sure=0.40)}   # 1536 차원으로 줄여 받은 벡터 기준
JUDGE_TIMEOUT = 4.0
CASE_POOL = 6        # 진입 후보 수. 어휘 점수에 밀려 정답 사례가 3위 밖에 있는 경우가 있다
WEAK_TOP = 3         # 가까운 후보가 하나도 없을 때 판정에 올리는 수
SURE_MARGIN = 0.05   # 다른 체인의 후보가 이 거리 안에 뒤따르면 1위를 판정 없이 채택하지 않는다


def _relevant(hit: dict, floor: CaseFloor | None) -> bool:
    """에러 코드 일치, 벡터 거리가 가까움, 조금 멀어도 질의 단어가 여럿 일치 — 셋 중 하나면 후보로 둔다. 벡터가 없으면 판단하지 않는다."""
    if floor is None or hit["exact"] or hit["distance"] is None:
        return True
    return hit["distance"] <= floor.near or (hit["distance"] <= floor.far and hit["lex_terms"] >= floor.terms)


def _sure(hit: dict, floor: CaseFloor | None) -> bool:
    """판정 없이 채택할 수 있는 후보: 에러 코드 일치이거나 아주 가깝다. 규정을 거쳐 온 후보(linked)와 멀리 있는 후보(weak)는 항상 판정한다."""
    if hit.get("linked") or hit.get("weak"):
        return False
    return floor is None or hit["exact"] or hit["distance"] is None or hit["distance"] <= floor.sure


def _chain_of(hit: dict) -> int:
    return hit.get("root_id") or hit["node_id"]


def _settled(hits: list[dict], floor: CaseFloor | None) -> bool:
    """1위를 판정 없이 채택해도 되는가. 가깝더라도 다른 체인의 후보가 근소하게 뒤따르면 판정에 맡긴다 — 거리만으로는 둘을 가르지 못한다."""
    top = hits[0]
    if not _sure(top, floor):
        return False
    if floor is None or top["exact"] or top["distance"] is None:
        return True
    return not any(_chain_of(h) != _chain_of(top) and h["distance"] is not None and h["distance"] <= top["distance"] + SURE_MARGIN
                   for h in hits[1:])


def _with_briefs(conn, ws: int, hits: list[dict]) -> list[dict]:
    """판정에 보여 줄 제목 · 요약과 체인 루트를 붙인다."""
    briefs = db.case_briefs(conn, ws, [h["node_id"] for h in hits])
    return [{**h, **briefs[h["node_id"]]} for h in hits if h["node_id"] in briefs]


def _case_candidates(ws: int, q_tokens: str, q_vec: list[float] | None, error_code: str | None, floor: CaseFloor | None,
                     linked_to: list[tuple[str, str]] | None = None) -> list[dict]:
    judged = floor is not None and q_vec is not None   # 판정할 수 있는가(보정된 모델 + 벡터) — 판정 없이는 후보를 넓히지 않는다
    with db.connect() as conn:
        found = db.search_case_entry(conn, ws, q_tokens, q_vec, error_code, CASE_POOL)
        hits = [h for h in found if _relevant(h, floor)]
        # 가까운 후보가 없으면 상위 몇 건을 판정에 맡긴다. 질문이 기록과 다른 말(줄임말 · 업계 용어 · 구어)을 쓰면 같은 문제도 멀게 나온다
        if not hits and judged:
            hits = [{**h, "weak": True} for h in found[:WEAK_TOP]]
        if hits and judged:
            hits = _with_briefs(conn, ws, hits)
        # 검색된 조문을 근거로 적어 둔 사례를 뒤에 더한다
        if linked_to and judged and not (hits and _settled(hits, floor)):
            have = {h["node_id"] for h in hits}
            hits += _with_briefs(conn, ws, [{"node_id": i, "score": 0.0, "exact": False, "lex_terms": 0, "distance": None, "linked": True}
                                            for i in db.cases_linked_to(conn, ws, linked_to) if i not in have])
    return hits


async def _choose_entry(prof, query: str, hits: list[dict], floor: CaseFloor | None, usage) -> int | None:
    """후보 중 진입 노드. 확실한 1위가 아니면 가벼운 모델이 후보마다 같은 문제인지 본다(병렬).
    통과한 후보가 여러 체인에 걸치면 질문과 가장 가까운 것을 한 번 더 고르게 하고, 그 밖에는 통과한 첫 후보를 쓴다.
    1위 후보의 판정이 실패하면 순위를 그대로 믿는다 — 멀리 있는 후보(weak)는 판정 없이 채택하지 않는다."""
    if not hits:
        return None
    if _settled(hits, floor):
        return hits[0]["node_id"]

    async def judge(h: dict) -> bool | None:
        try:
            return await asyncio.wait_for(llm.judge_case(prof, query, h, usage), JUDGE_TIMEOUT)
        except Exception:  # noqa: BLE001
            return None

    verdicts = await asyncio.gather(*(judge(h) for h in hits))
    if verdicts[0] is None:
        return None if hits[0].get("weak") else hits[0]["node_id"]
    passed = [h for h, v in zip(hits, verdicts) if v]
    if len({_chain_of(h) for h in passed}) > 1:
        try:
            return passed[await asyncio.wait_for(llm.pick_case(prof, query, passed, usage), JUDGE_TIMEOUT)]["node_id"]
        except Exception:  # noqa: BLE001 — 고르지 못하면 순위대로
            pass
    return passed[0]["node_id"] if passed else None


async def _graph_search(ws: int, q_tokens: str, q_vec: list[float] | None, error_code: str | None,
                        floor: CaseFloor | None = None, prof=None, query: str = "", usage=None,
                        linked_to: list[tuple[str, str]] | None = None) -> tuple[list[dict], ChainEvent | None]:
    hits = await asyncio.to_thread(_case_candidates, ws, q_tokens, q_vec, error_code, floor, linked_to)
    entry = await _choose_entry(prof, query, hits, floor, usage)
    if entry is None:
        return [], None
    return await asyncio.to_thread(_load_chain, ws, entry)


def _same_line(entry: int, edges: list[dict]) -> set[int]:
    """진입 노드와 같은 줄기: 진입 노드, 그 조상, 그 자손. 같은 체인의 다른 가지는 빠진다."""
    up, down = {}, {}
    for e in edges:
        up.setdefault(e["child_id"], []).append(e["parent_id"])
        down.setdefault(e["parent_id"], []).append(e["child_id"])
    seen = {entry}
    for link in (up, down):
        todo = [entry]
        while todo:
            for nxt in link.get(todo.pop(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
    return seen


def _load_chain(ws: int, entry: int) -> tuple[list[dict], ChainEvent]:
    with db.connect() as conn:
        # 루트 기준 체인: 진입 노드의 루트에서 하향, 깊이 제한 없이 30개까지 — 넘치면 진입 · 해결 · 원인 경로 먼저
        root = db.root_of(conn, ws, entry) or entry
        ch = db.chain(conn, ws, root, None, 30, focus=entry)
        resolving = db.resolved_nodes(conn, ws, root)
    nodes = sorted(ch["nodes"], key=lambda n: (n["depth"], n["id"]))  # 마커 번호 = depth 안정 정렬 순서
    near = _same_line(entry, ch["edges"])
    via: dict[int, list[str]] = {}   # 기록 → 상위에 이어 붙인 이유
    for e in ch["edges"]:   # 상위가 여럿이면 이유도 여럿 — 같은 문장은 한 번만
        if e.get("reason") and e["reason"] not in via.setdefault(e["child_id"], []):
            via[e["child_id"]].append(e["reason"])
    refs = {n["id"]: f"N{i + 1}" for i, n in enumerate(nodes)}
    parents: dict[int, list[str]] = {}   # 기록 → 체인 안의 바로 위 기록 마커 (깊이순 나열이라 들여쓰기만으로는 어느 원인 아래인지 안 보인다)
    for e in ch["edges"]:
        if e["parent_id"] in refs:
            parents.setdefault(e["child_id"], []).append(refs[e["parent_id"]])
    # 갈라지는 체인(깊이 2 이상 기록의 상위가 둘 이상)일 때만 표시한다 — 한 줄기 체인에 표시를 더하면 답변에 뜻 없는 문장이 늘었다
    depth = {n["id"]: n["depth"] for n in nodes}
    if len({r for c, ps in parents.items() if depth.get(c, 0) >= 2 for r in ps}) < 2:
        parents = {}
    ctx, ev_nodes = [], []
    for n in nodes:
        ref, cause = refs[n["id"]], bool(n.get("is_cause"))
        ctx.append({"ref": ref, "id": n["id"], "type": n["type"], "status": n["status"], "title": n["title"],
                    "resolution_summary": n.get("resolution_summary"), "depth": n["depth"], "policy_refs": n.get("policy_refs"),
                    "entry": n["id"] == entry, "near": n["id"] in near, "resolves_root": n["id"] in resolving, "via": via.get(n["id"], []),
                    "cause": cause, "parents": sorted(parents.get(n["id"], []), key=lambda r: int(r[1:]))})
        ev_nodes.append(ChainNode(ref=ref, id=n["id"], type=n["type"], status=n["status"], title=n["title"], depth=n["depth"],
                                  path_label=n.get("path_label"),
                                  last_confirmed_at=n["last_confirmed_at"].isoformat() if n.get("last_confirmed_at") else None,
                                  resolves_root=n["id"] in resolving, is_cause=cause))
    return ctx, ChainEvent(entry_id=entry, nodes=ev_nodes, edges=[ChainEdge(**e) for e in ch["edges"]])


def _done(intent, retrieved, verified_ids, unverified_refs, retrieval_ms, first_token_ms, t0, model, usage, truncated=False) -> DoneEvent:
    return DoneEvent(intent=intent, retrieved_ids=retrieved, verified_ids=verified_ids, unverified_ids=unverified_refs,
                     latency=Latency(retrieval_ms=retrieval_ms, first_token_ms=first_token_ms, total_ms=int((time.perf_counter() - t0) * 1000)),
                     model=model, prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens, truncated=truncated)
