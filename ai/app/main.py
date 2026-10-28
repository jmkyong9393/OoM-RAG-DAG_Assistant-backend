"""AI 서비스 FastAPI 래퍼. /ai/chat 은 AI_MODE 에 따라 더미 스트림(E1 스파이크용) 또는 실제 파이프라인(graph/pipeline.py)을 흘린다.
이벤트 8종·순서는 app/sse.py · 개발문서 04 §4.3 이 정본이고, `session` 은 Spring 이 앞에 붙인다.

실행: uv run uvicorn app.main:app --port 18000 --reload
"""
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import ai_mode, profile
from app.graph import pipeline
from app.sse import sse

@asynccontextmanager
async def lifespan(_: FastAPI):
    # kiwi 모델 로드 + 전체 워크스페이스 사용자 단어 추가 + 첫 분석(사전 빌드 ≈1.4s)을 첫 요청이 아니라 기동 시에 끝낸다
    if ai_mode() != "dummy":
        from app.retrieval import db
        from app.retrieval.tokenize import warm_up

        def _warm():
            terms: list[str] = []
            try:
                with db.connect() as conn:
                    terms = [r["term"] for r in conn.execute("SELECT DISTINCT term FROM workspace_terms")]
            except Exception:  # noqa: BLE001 — DB 없이도 기동은 되게
                pass
            return warm_up(terms)
        await asyncio.to_thread(_warm)
    yield


app = FastAPI(title="OoM Assist AI", version="0.2.0", lifespan=lifespan)
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    workspace_id: int
    policy_enabled: bool | None = None
    session_id: int | None = None
    query: str


class ChainSummaryRequest(BaseModel):
    nodes: list[dict]
    edges: list[dict]


@app.get("/ai/health")
def health():
    mode = ai_mode()
    prof = profile()
    db_ok = False
    try:
        from app.retrieval import db
        with db.connect() as conn:
            db_ok = conn.execute("SELECT 1").fetchone() is not None
    except Exception:  # noqa: BLE001
        db_ok = False
    models = {"light": prof.model_light, "answer": prof.model_answer, "embedding": prof.embedding_model} if mode == "openai" else {}
    return {"status": "ok", "mode": mode, "models": models, "db": db_ok}


@app.post("/ai/chat")
async def chat(req: ChatRequest):
    if ai_mode() == "dummy":
        gen = dummy_stream(req)
    else:
        gen = pipeline.run(req.workspace_id, req.policy_enabled, req.query, req.session_id)
    return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/ai/chain-summary")
def chain_summary(req: ChainSummaryRequest):
    titles = [n.get("title", "") for n in req.nodes]
    return {"summary": " → ".join(titles) if titles else ""}


async def dummy_stream(req: ChatRequest):
    """고정 이벤트. DB·LLM 없이 릴레이·프론트 훅·세션 기록을 검증하는 용도."""
    t0 = time.perf_counter()
    policy_enabled = bool(req.policy_enabled)
    mode = "policy" if policy_enabled else "dag_only"
    intent = "both" if policy_enabled else "troubleshoot"
    yield sse("stage", {"node": "supervisor", "status": "start"})
    await asyncio.sleep(0.05)
    yield sse("intent", {"intent": intent, "mode": mode})
    yield sse("stage", {"node": "supervisor", "status": "done"})
    policy = []
    if policy_enabled:
        yield sse("stage", {"node": "policy_rag", "status": "start"})
        policy = [{"ref": "P1", "chunk_id": "vpn-guide-v21-a12-1", "doc_id": 1, "doc_title": "VPN·원격접속 운영지침",
                   "version": "v2.1", "article_label": "제12조 (원격접속 신청)", "page": 7,
                   "text": "외근 시 원격접속은 사전 신청 후 승인된 기간에만 허용한다."}]
        yield sse("policy", policy)
        yield sse("stage", {"node": "policy_rag", "status": "done"})
    yield sse("stage", {"node": "graph_search", "status": "start"})
    chain = {"entry_id": 101, "nodes": [
        {"ref": "N1", "id": 101, "type": "fail", "status": "active", "title": "VPN 접속 시 E-403 반복 발생", "depth": 0, "path_label": "root.n101", "last_confirmed_at": None},
        {"ref": "N2", "id": 106, "type": "ref", "status": "active", "title": "토큰 갱신 배치 중단", "depth": 1, "path_label": "root.n101.n106", "last_confirmed_at": None},
        {"ref": "N3", "id": 108, "type": "success", "status": "active", "title": "배치 주기 30분으로 변경", "depth": 2, "path_label": "root.n101.n106.n108", "last_confirmed_at": None},
    ], "edges": [{"parent_id": 101, "child_id": 106}, {"parent_id": 106, "child_id": 108}]}
    yield sse("chain", chain)
    yield sse("stage", {"node": "graph_search", "status": "done"})
    retrieval_ms = int((time.perf_counter() - t0) * 1000)
    yield sse("stage", {"node": "gate", "status": "done"})
    yield sse("stage", {"node": "generate", "status": "start"})
    first_token_ms = None
    answer = ("E-403은 토큰 갱신 배치가 중단되어 발생한 사례가 있습니다 [N1]. 원인은 배치 중단 [N2]이었고, "
              "배치 주기를 30분으로 조정해 해결했습니다 [N3]." + (" 외근 시 원격접속은 사전 신청이 필요합니다 [P1]." if policy else ""))
    for piece in answer.split(" "):
        if first_token_ms is None:
            first_token_ms = int((time.perf_counter() - t0) * 1000)
        yield sse("token", {"text": piece + " "})
        await asyncio.sleep(0.02)
    yield sse("stage", {"node": "generate", "status": "done"})
    yield sse("stage", {"node": "critic", "status": "start"})
    verified = ["N1", "N2", "N3"] + (["P1"] if policy else [])
    yield sse("citations", {"verified": verified, "unverified": []})
    yield sse("stage", {"node": "critic", "status": "done"})
    ids = ["n:101", "n:106", "n:108"] + (["c:vpn-guide-v21-a12-1"] if policy else [])
    yield sse("done", {"intent": intent, "retrieved_ids": ids, "verified_ids": ids, "unverified_ids": [],
                       "latency": {"retrieval_ms": retrieval_ms, "first_token_ms": first_token_ms or 0, "total_ms": int((time.perf_counter() - t0) * 1000)},
                       "model": "dummy", "prompt_tokens": 0, "completion_tokens": 0})
