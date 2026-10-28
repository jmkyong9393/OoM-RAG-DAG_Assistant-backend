"""DB 접근 계층. 원시 SQL + 컬럼 상수만 쓴다(ORM 금지, 개발문서 34 §5). 검색·체인은 Flyway 함수를 호출한다.
계정 oom_ai 는 SELECT + 함수 EXECUTE + log_nodes 정제 컬럼 UPDATE + policy_* INSERT/UPDATE + workspace_terms INSERT 만 가능하다.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from app.config import db_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url(), row_factory=dict_row) as conn:
        yield conn


def vec_literal(vec: list[float] | None) -> str | None:
    return None if vec is None else "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# ── 워크스페이스 ────────────────────────────────────────────────────────────

def workspace_settings(conn, ws: int) -> dict[str, Any]:
    row = conn.execute("SELECT policy_enabled, settings FROM workspaces WHERE id = %s", (ws,)).fetchone()
    if not row:
        return {"policy_enabled": False, "settings": {}}
    return {"policy_enabled": row["policy_enabled"], "settings": row["settings"] or {}}


def workspace_terms(conn, ws: int) -> list[str]:
    return [r["term"] for r in conn.execute("SELECT term FROM workspace_terms WHERE workspace_id = %s", (ws,))]


# ── 검색 (DB 함수) ───────────────────────────────────────────────────────────

def search_chunks(conn, ws: int, tokens: str | None, vec: list[float] | None, k: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT chunk_id, score FROM hybrid_search_chunks(%s, %s, %s::vector, %s)",
        (ws, tokens, vec_literal(vec), k)).fetchall()
    if not rows:
        return []
    ids = [r["chunk_id"] for r in rows]
    detail = {r["id"]: r for r in conn.execute("""
        SELECT c.id, c.doc_id, d.title AS doc_title, d.version, c.article_label, c.page, c.text
        FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id WHERE c.id = ANY(%s)""", (ids,))}
    return [{**detail[i], "score": s["score"]} for i, s in zip(ids, rows) if i in detail]


def search_nodes(conn, ws: int, tokens: str | None, vec: list[float] | None, error_code: str | None, k: int = 5) -> list[dict]:
    return conn.execute(
        "SELECT node_id, score FROM hybrid_search_nodes(%s, %s, %s::vector, %s, %s)",
        (ws, tokens, vec_literal(vec), error_code, k)).fetchall()


def chain(conn, ws: int, entry: int, max_depth: int = 3, max_nodes: int = 30) -> dict:
    rows = conn.execute("""
        SELECT v.id, v.type, v.status, v.title, v.body, v.resolution_summary, v.error_code, v.author_name,
               g.depth, v.path_label, v.last_confirmed_at, v.root_id
        FROM get_chain(%s, %s, %s, %s) g JOIN v_log_nodes v ON v.id = g.node_id
        ORDER BY g.depth, v.id""", (ws, entry, max_depth, max_nodes)).fetchall()
    if not rows:
        return {"entry_id": entry, "nodes": [], "edges": []}
    ids = [r["id"] for r in rows]
    edges = conn.execute(
        "SELECT parent_id, child_id FROM log_edges WHERE parent_id = ANY(%s) AND child_id = ANY(%s) ORDER BY parent_id, child_id",
        (ids, ids)).fetchall()
    return {"entry_id": entry, "nodes": rows, "edges": edges}


def root_of(conn, ws: int, node_id: int) -> int | None:
    row = conn.execute("SELECT root_id FROM v_log_nodes WHERE workspace_id = %s AND id = %s", (ws, node_id)).fetchone()
    return row["root_id"] if row else None


# ── 정제 워커 ────────────────────────────────────────────────────────────────

def claim_pending_log(conn, exclude: list[int] | None = None) -> dict | None:
    """pending 1건을 processing 으로 바꾸며 집는다. 같은 행을 두 워커가 집지 않도록 SKIP LOCKED.
    exclude: 이번 주기에 이미 시도한 ID (실패 재시도는 다음 폴링 주기로 미룬다)."""
    row = conn.execute("""
        WITH picked AS (
          SELECT id FROM log_nodes WHERE refine_status = 'pending' AND refine_attempts < 3 AND NOT (id = ANY(%s))
          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
        UPDATE log_nodes n SET refine_status = 'processing' FROM picked
        WHERE n.id = picked.id
        RETURNING n.id, n.workspace_id, n.type, n.title, n.body, n.refine_attempts""", (exclude or [],)).fetchone()
    conn.commit()
    return row


def finish_refine(conn, node_id: int, result: dict, tokens: str, vec: list[float] | None, embedding_model: str | None) -> None:
    conn.execute("""
        UPDATE log_nodes SET error_code = %s, keywords = %s, resolution_summary = %s, tokens = %s,
          embedding = %s::vector, embedding_model = %s, refine_status = 'done', refine_error = NULL
        WHERE id = %s""",
        (result.get("error_code"), result.get("keywords", []), result.get("resolution_summary"), tokens,
         vec_literal(vec), embedding_model, node_id))
    conn.commit()


def fail_refine(conn, node_id: int, attempts: int, error: str, tokens: str | None) -> None:
    status = "failed" if attempts + 1 >= 3 else "pending"
    conn.execute("UPDATE log_nodes SET refine_status = %s, refine_attempts = %s, refine_error = %s, tokens = coalesce(%s, tokens) WHERE id = %s",
                 (status, attempts + 1, error[:500], tokens, node_id))
    conn.commit()


def add_terms(conn, ws: int, terms: list[str]) -> None:
    for t in {x.strip() for x in terms if x and x.strip()}:
        conn.execute("INSERT INTO workspace_terms (workspace_id, term, source) VALUES (%s, %s, 'auto') ON CONFLICT DO NOTHING", (ws, t))
    conn.commit()


def nodes_without_tokens(conn, limit: int = 200) -> list[dict]:
    """시드·수동 입력으로 refine 은 끝났지만 tokens 가 비어 있는 노드 (어휘 검색 대상이 되게 백필)."""
    return conn.execute("""SELECT id, workspace_id, title, body, keywords, error_code FROM log_nodes
                           WHERE tokens IS NULL AND refine_status <> 'pending' ORDER BY id LIMIT %s""", (limit,)).fetchall()


def set_node_tokens(conn, node_id: int, tokens: str, vec: list[float] | None, embedding_model: str | None) -> None:
    conn.execute("UPDATE log_nodes SET tokens = %s, embedding = coalesce(%s::vector, embedding), embedding_model = coalesce(%s, embedding_model) WHERE id = %s",
                 (tokens, vec_literal(vec), embedding_model, node_id))
    conn.commit()


def chunks_without_tokens(conn, limit: int = 200) -> list[dict]:
    """문서 제목·조문 라벨도 함께 돌려준다 — 핵심어(예: 'VPN')가 본문이 아니라 제목·라벨에만 있는 청크가 많다."""
    return conn.execute("""SELECT c.id, d.title AS doc_title, c.article_label, c.text
                           FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
                           WHERE c.tokens IS NULL ORDER BY c.id LIMIT %s""", (limit,)).fetchall()


def reset_tokens(conn) -> None:
    """토크나이저가 바뀌었을 때 전부 다시 만들게 tokens 를 비운다 (worker --reindex)."""
    conn.execute("UPDATE policy_chunks SET tokens = NULL")
    conn.execute("UPDATE log_nodes SET tokens = NULL WHERE refine_status <> 'pending'")
    conn.commit()


def set_chunk_tokens(conn, chunk_id: str, tokens: str, vec: list[float] | None, embedding_model: str | None) -> None:
    conn.execute("UPDATE policy_chunks SET tokens = %s, embedding = coalesce(%s::vector, embedding), meta = meta || %s::jsonb WHERE id = %s",
                 (tokens, vec_literal(vec), json.dumps({"embedding_model": embedding_model} if vec else {}), chunk_id))
    conn.commit()
