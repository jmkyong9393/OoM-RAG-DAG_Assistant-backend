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
def connect(timeout: float | None = None) -> Iterator[psycopg.Connection]:
    """timeout: 접속 대기 상한(초). 기동 워밍업처럼 DB 가 없어도 진행해야 하는 자리에서만 준다 — 없으면 SYN 이 버려지는 환경에서 무한 대기한다."""
    kw = {"connect_timeout": int(timeout)} if timeout else {}
    with psycopg.connect(db_url(), row_factory=dict_row, **kw) as conn:
        yield conn


def vec_literal(vec: list[float] | None) -> str | None:
    return None if vec is None else "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# ── 워크스페이스 ────────────────────────────────────────────────────────────

def workspace_settings(conn, ws: int) -> dict[str, Any]:
    row = conn.execute("SELECT policy_enabled, case_enabled, settings FROM workspaces WHERE id = %s", (ws,)).fetchone()
    if not row:
        return {"policy_enabled": False, "case_enabled": True, "settings": {}}
    return {"policy_enabled": row["policy_enabled"], "case_enabled": row["case_enabled"], "settings": row["settings"] or {}}


def classify_context(conn, ws: int, error_code: str | None) -> dict[str, bool]:
    """의도 분류에 쓰는 부서 사정: 활성 사례가 하나라도 있는가(has_cases), 질의의 에러 코드를 검색 범위의 규정이 다루는가(code_in_policy)."""
    has_cases = conn.execute("SELECT EXISTS (SELECT 1 FROM log_nodes WHERE workspace_id = %s AND status = 'active') AS x", (ws,)).fetchone()["x"]
    code_in_policy = False
    if error_code:
        code_in_policy = conn.execute("""SELECT EXISTS (SELECT 1 FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
                                         WHERE d.status = 'active' AND c.workspace_id IN (SELECT workspace_ancestors(%s)) AND c.text ILIKE %s) AS x""",
                                      (ws, f"%{error_code}%")).fetchone()["x"]
    return {"has_cases": bool(has_cases), "code_in_policy": bool(code_in_policy)}


def user_terms(conn) -> list[str]:
    """kiwi 사용자 사전 = workspace_terms 전체의 합집합. kiwi 의 add_user_word 는 프로세스 전역이라 워크스페이스별로 나눠 봐도
    먼저 읽은 사전이 뒤에 영향을 준다 — 그래서 질의·청크·노드·평가가 전부 같은 합집합을 쓴다(용어는 분절 규칙일 뿐 검색 범위가 아니다)."""
    return [r["term"] for r in conn.execute("SELECT DISTINCT term FROM workspace_terms ORDER BY term")]


# ── 검색 (DB 함수) ───────────────────────────────────────────────────────────

def search_chunks(conn, ws: int, tokens: str | None, vec: list[float] | None, k: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT chunk_id, score FROM hybrid_search_chunks(%s, %s, %s::vector, %s)",
        (ws, tokens, vec_literal(vec), k)).fetchall()
    if not rows:
        return []
    ids = [r["chunk_id"] for r in rows]
    detail = {r["id"]: r for r in conn.execute("""
        SELECT c.id, c.doc_id, d.slug, d.title AS doc_title, d.version, d.doc_type, c.article_label, c.page, c.text
        FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id WHERE c.id = ANY(%s)""", (ids,))}
    return [{**detail[i], "score": s["score"]} for i, s in zip(ids, rows) if i in detail]


def search_nodes(conn, ws: int, tokens: str | None, vec: list[float] | None, error_code: str | None, k: int = 5) -> list[dict]:
    return conn.execute(
        "SELECT node_id, score FROM hybrid_search_nodes(%s, %s, %s::vector, %s, %s)",
        (ws, tokens, vec_literal(vec), error_code, k)).fetchall()


def search_case_entry(conn, ws: int, tokens: str | None, vec: list[float] | None, error_code: str | None, k: int = 3) -> list[dict]:
    """어시스턴트용 사례 진입 후보. 각 행에 관련성 근거(exact · lex_terms · distance)가 붙는다."""
    return conn.execute(
        "SELECT node_id, score, exact, lex_terms, distance FROM search_case_entry(%s, %s, %s::vector, %s, %s)",
        (ws, tokens, vec_literal(vec), error_code, k)).fetchall()


def chain(conn, ws: int, entry: int, max_depth: int = 3, max_nodes: int = 30) -> dict:
    rows = conn.execute("""
        SELECT v.id, v.type, v.status, v.title, v.body, v.resolution_summary, v.error_code, v.author_name,
               g.depth, v.path_label, v.last_confirmed_at, v.root_id, v.attributes->'policy_refs' AS policy_refs
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


def same_article(label: str, article: str) -> bool:
    """청크 라벨('제12조 (접근 토큰과 서비스 계정)', '[별표 2] 배포 전 점검표')이 그 조문('제12조', '별표 2')인가."""
    head = (label or "").strip().lstrip("[")
    return head.startswith(article) and not head[len(article):len(article) + 1].isdigit()


def policy_ref_chunks(conn, ws: int, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """(문서 slug, 조문) → 그 조문의 첫 청크. 이 부서에서 검색되는 활성 문서만."""
    if not keys:
        return {}
    rows = conn.execute("""
        SELECT c.id, c.doc_id, d.slug, d.title AS doc_title, d.version, d.doc_type, c.article_label, c.page, c.text
        FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
        WHERE d.status = 'active' AND d.slug = ANY(%s) AND c.workspace_id IN (SELECT workspace_ancestors(%s))
        ORDER BY c.id""", ([k[0] for k in keys], ws)).fetchall()
    out = {}
    for slug, article in keys:
        hit = next((r for r in rows if r["slug"] == slug and same_article(r["article_label"], article)), None)
        if hit:
            out[(slug, article)] = hit
    return out


def cases_linked_to(conn, ws: int, keys: list[tuple[str, str]], k: int = 3) -> list[int]:
    """그 조문을 근거로 적어 둔 활성 사례. 규정과 달리 한 사례(exception)를 먼저, 그다음 최근 것."""
    if not keys:
        return []
    rows = conn.execute("""
        SELECT n.id, r->>'policy' AS policy, r->>'article' AS article, r->>'relation' AS relation
        FROM log_nodes n CROSS JOIN LATERAL jsonb_array_elements(
               CASE WHEN jsonb_typeof(n.attributes->'policy_refs') = 'array' THEN n.attributes->'policy_refs' ELSE '[]'::jsonb END) r
        WHERE n.workspace_id = %s AND n.status = 'active' AND r->>'policy' = ANY(%s)
        ORDER BY (r->>'relation' = 'exception') DESC, n.id DESC""", (ws, [k_[0] for k_ in keys])).fetchall()
    want = set(keys)
    return list(dict.fromkeys(r["id"] for r in rows if (r["policy"], r["article"]) in want))[:k]


def case_briefs(conn, ws: int, node_ids: list[int]) -> dict[int, dict]:
    """관련성 판정에 보여 줄 요약: 체인 루트 제목 · 그 기록의 제목 · 해결 요약(없으면 본문 앞부분)."""
    rows = conn.execute("""
        SELECT v.id, v.title, coalesce(v.resolution_summary, left(v.body, 200)) AS summary, r.title AS root_title
        FROM v_log_nodes v JOIN log_nodes r ON r.id = coalesce(v.root_id, v.id)
        WHERE v.workspace_id = %s AND v.id = ANY(%s)""", (ws, node_ids)).fetchall()
    return {r["id"]: {"title": r["title"], "summary": r["summary"], "root_title": r["root_title"]} for r in rows}


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
    return conn.execute("""SELECT c.id, c.workspace_id, d.title AS doc_title, c.article_label, c.text, c.embedding IS NULL AS needs_vec
                           FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
                           WHERE c.tokens IS NULL ORDER BY c.id LIMIT %s""", (limit,)).fetchall()


def reset_tokens(conn, embedding_model: str | None = None) -> None:
    """토크나이저가 바뀌었을 때 전부 다시 만들게 tokens 를 비운다 (worker --reindex).
    embedding_model 을 주면 다른 모델로 만든 청크 벡터도 비워 다시 만들게 한다 (노드 벡터는 재색인 때 늘 다시 만든다)."""
    conn.execute("UPDATE policy_chunks SET tokens = NULL")
    if embedding_model:
        conn.execute("UPDATE policy_chunks SET embedding = NULL WHERE embedding IS NOT NULL AND meta->>'embedding_model' IS DISTINCT FROM %s",
                     (embedding_model,))
    conn.execute("UPDATE log_nodes SET tokens = NULL WHERE refine_status <> 'pending'")
    conn.commit()


def embedding_mismatch(conn, embedding_model: str) -> bool:
    """저장된 벡터 중 지금 모델이 아닌 모델로 만든 것이 있는가. 모델이 다르면 거리가 뜻이 없다."""
    return conn.execute("""
        SELECT EXISTS (SELECT 1 FROM policy_chunks WHERE embedding IS NOT NULL AND meta->>'embedding_model' IS DISTINCT FROM %(m)s)
            OR EXISTS (SELECT 1 FROM log_nodes WHERE embedding IS NOT NULL AND embedding_model IS DISTINCT FROM %(m)s) AS stale""",
        {"m": embedding_model}).fetchone()["stale"]


def set_chunk_tokens(conn, chunk_id: str, tokens: str, title_tokens: str, vec: list[float] | None, embedding_model: str | None) -> None:
    """tokens(라벨+본문, 가중 A)와 title_tokens(문서 제목, 가중 D)는 tsv 생성 컬럼이 합친다 (V7)."""
    conn.execute("UPDATE policy_chunks SET tokens = %s, title_tokens = %s, embedding = coalesce(%s::vector, embedding), meta = meta || %s::jsonb WHERE id = %s",
                 (tokens, title_tokens, vec_literal(vec), json.dumps({"embedding_model": embedding_model} if vec else {}), chunk_id))
    conn.commit()
