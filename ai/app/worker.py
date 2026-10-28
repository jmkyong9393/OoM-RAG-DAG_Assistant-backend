"""정제 워커. log_nodes.refine_status = pending 을 폴링해(SKIP LOCKED) LLM 정제 → kiwi 토큰 → 임베딩 → UPDATE.
실패는 attempts+1, 3회면 failed. 키워드·에러 코드는 workspace_terms(auto) 에 누적한다.
부수 작업: tokens 가 비어 있는 policy_chunks·log_nodes(정제 완료분)도 토크나이징·임베딩한다(시드 직후 어휘 검색이 바로 되게).

실행: uv run python -m app.worker            (무한 루프)
      uv run python -m app.worker --once     (대기 작업만 처리하고 종료 — 테스트·시드 후 1회)
      uv run python -m app.worker --reindex  (토큰·임베딩 전부 재생성 — 토크나이저/임베딩 모델 변경 후)
"""
from __future__ import annotations

import asyncio
import sys
import time

from app.config import ai_mode, profile as load_profile, worker_poll_sec
from app.llm import client as llm
from app.retrieval import db, embed
from app.retrieval.tokenize import Tokenizer


async def refine_one(conn, row: dict, tokenizer_cache: dict[int, Tokenizer]) -> bool:
    ws = row["workspace_id"]
    tok = tokenizer_cache.get(ws)
    if tok is None:
        tok = tokenizer_cache[ws] = Tokenizer(db.workspace_terms(conn, ws))
    text = f"{row['title']}\n{row['body']}"
    tokens = tok.tokens(text)
    try:
        prof = load_profile(db.workspace_settings(conn, ws)["settings"].get("llm_profile", "default"))
        result = await llm.refine(prof, row["title"], row["body"], tok, llm.Usage())
        vec = await asyncio.to_thread(embed.embed_one, prof, text)
        # 키워드도 토큰에 섞어 어휘 검색이 정제 결과를 활용하게 한다
        kw_tokens = tok.tokens(" ".join(result.keywords)) if result.keywords else ""
        db.finish_refine(conn, row["id"], result.model_dump(), (tokens + " " + kw_tokens).strip(),
                         vec, prof.embedding_model if vec else None)
        db.add_terms(conn, ws, result.keywords + ([result.error_code] if result.error_code else []))
        return True
    except Exception as e:  # noqa: BLE001
        db.fail_refine(conn, row["id"], row["refine_attempts"], f"{type(e).__name__}: {e}", tokens)
        return False


async def backfill_chunk_tokens(conn, tokenizer: Tokenizer) -> int:
    rows = db.chunks_without_tokens(conn)
    if not rows:
        return 0
    prof = load_profile()
    # 임베딩은 "문서 제목 + 조문 라벨 + 본문", 어휘 토큰은 제목을 떼어 따로(가중 D) — 제목이 모든 청크에 들어가 순위를 흐리기 때문
    texts = [f"{r['doc_title']}\n{r['article_label']}\n{r['text']}" for r in rows]
    need = [i for i, r in enumerate(rows) if r["needs_vec"]]   # 토큰만 다시 만들 때(--reindex·V7) 임베딩은 재사용
    vecs: dict[int, list[float]] = {}
    if need and ai_mode() == "openai":
        got = await asyncio.to_thread(embed.embed, prof, [texts[i] for i in need])
        vecs = dict(zip(need, got))
    for i, r in enumerate(rows):
        v = vecs.get(i)
        db.set_chunk_tokens(conn, r["id"], tokenizer.tokens(f"{r['article_label']}\n{r['text']}"), tokenizer.tokens(r["doc_title"]),
                            v, prof.embedding_model if v else None)
    return len(rows)


async def backfill_node_tokens(conn, cache: dict[int, Tokenizer]) -> int:
    rows = db.nodes_without_tokens(conn)
    if not rows:
        return 0
    prof = load_profile()
    texts = [f"{r['title']}\n{r['body']}" for r in rows]
    vecs = await asyncio.to_thread(embed.embed, prof, texts) if ai_mode() == "openai" else None
    for i, r in enumerate(rows):
        ws = r["workspace_id"]
        tok = cache.get(ws) or cache.setdefault(ws, Tokenizer(db.workspace_terms(conn, ws)))
        extra = " ".join((r["keywords"] or []) + ([r["error_code"]] if r["error_code"] else []))
        db.set_node_tokens(conn, r["id"], tok.tokens(texts[i] + "\n" + extra), vecs[i] if vecs else None, prof.embedding_model if vecs else None)
    return len(rows)


async def run_once(reindex: bool = False) -> dict:
    stats = {"refined": 0, "failed": 0, "chunks": 0, "nodes": 0}
    cache: dict[int, Tokenizer] = {}
    with db.connect() as conn:
        if reindex:
            db.reset_tokens(conn)
        # 한 번에 200건씩(임베딩 요청 크기) 비어 있는 행이 없을 때까지 반복 — --reindex 직후 수백 건도 한 주기에 끝낸다
        tokenizer = Tokenizer()
        while (n := await backfill_chunk_tokens(conn, tokenizer)) > 0:
            stats["chunks"] += n
        while (n := await backfill_node_tokens(conn, cache)) > 0:
            stats["nodes"] += n
        tried: list[int] = []
        while True:
            row = db.claim_pending_log(conn, tried)
            if row is None:
                break
            tried.append(row["id"])
            ok = await refine_one(conn, row, cache)
            stats["refined" if ok else "failed"] += 1
    return stats


async def loop() -> None:
    print(f"[worker] mode={ai_mode()} poll={worker_poll_sec()}s", flush=True)
    while True:
        try:
            s = await run_once()
            if any(s.values()):
                print(f"[worker] {time.strftime('%H:%M:%S')} {s}", flush=True)
        except Exception as e:  # noqa: BLE001 — DB 끊김 등은 다음 주기에 재시도
            print(f"[worker] error: {e}", flush=True)
        await asyncio.sleep(worker_poll_sec())


if __name__ == "__main__":
    if "--reindex" in sys.argv:          # 토크나이저·임베딩 모델 변경 후: 청크·노드 토큰(+임베딩) 전부 재생성
        print(asyncio.run(run_once(reindex=True)))
    elif "--once" in sys.argv:
        print(asyncio.run(run_once()))
    else:
        asyncio.run(loop())
