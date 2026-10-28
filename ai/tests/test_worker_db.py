"""정제 워커 통합 테스트 — 관리자 계정으로 pending 노드를 넣고, 앱 계정(oom_ai)의 권한만으로 정제·백필이 되는지 본다."""
import pytest

from app import worker
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def pending_node(admin_conn):
    row = admin_conn.execute("""
        INSERT INTO log_nodes (workspace_id, type, status, title, body, author_id, refine_status, source)
        VALUES (1, 'fail', 'active', '[test] 결제 모듈 E-500 장애', '결제 승인 단계에서 E-500 이 반복 발생. 캐시 서버를 재시작해 복구했다.', 2, 'pending', 'synthetic')
        RETURNING id""").fetchone()
    before = {r["term"] for r in admin_conn.execute("SELECT term FROM workspace_terms WHERE workspace_id = 1")}
    yield row["id"]
    admin_conn.execute("DELETE FROM log_nodes WHERE id = %s", (row["id"],))
    admin_conn.execute("DELETE FROM workspace_terms WHERE workspace_id = 1 AND source = 'auto' AND term <> ALL(%s)", (list(before),))


async def test_worker_refines_pending_node_and_backfills(fake_mode, pending_node, admin_conn):
    stats = await worker.run_once()
    assert stats["refined"] >= 1 and stats["failed"] == 0
    n = admin_conn.execute("SELECT * FROM log_nodes WHERE id = %s", (pending_node,)).fetchone()
    assert n["refine_status"] == "done" and n["refine_error"] is None
    assert n["error_code"] == "E-500"
    assert n["keywords"] and "결제" in n["keywords"]
    assert n["resolution_summary"].startswith("결제 승인 단계에서")
    assert n["tokens"] and "e-500" in n["tokens"].split()
    assert n["tsv"] is not None
    assert n["embedding"] is None, "fake 모드에서는 임베딩을 만들지 않는다"
    terms = {r["term"] for r in admin_conn.execute("SELECT term FROM workspace_terms WHERE workspace_id = 1 AND source = 'auto'")}
    assert "E-500" in terms and "결제" in terms
    # 백필 결과: 시드 노드·청크에 tokens 가 모두 채워져 있다
    assert admin_conn.execute("SELECT count(*) AS c FROM policy_chunks WHERE tokens IS NULL OR title_tokens IS NULL").fetchone()["c"] == 0
    # 제목 토큰은 D(기본, 표기 없음)·라벨+본문은 A 가중 — 문서 제목만의 토큰 '지침' 은 A 가 붙지 않는다 (V7)
    import re
    tsv = admin_conn.execute("SELECT tsv::text AS t, tokens, title_tokens FROM policy_chunks WHERE id = 'vpn-guide-v22-a15-1'").fetchone()
    assert "지침" in tsv["title_tokens"].split() and "지침" not in tsv["tokens"].split()
    assert re.search(r"'토큰':\d+A", tsv["t"]) and re.search(r"'지침':\d+(?![0-9A-C])", tsv["t"]), tsv["t"]
    assert admin_conn.execute("SELECT count(*) AS c FROM log_nodes WHERE tokens IS NULL AND refine_status <> 'pending'").fetchone()["c"] == 0
    # 두 번째 실행은 할 일이 없다
    again = await worker.run_once()
    assert again == {"refined": 0, "failed": 0, "chunks": 0, "nodes": 0}


async def test_refined_node_is_searchable_by_error_code_and_words(fake_mode, pending_node, admin_conn):
    await worker.run_once()
    from app.retrieval import db
    with db.connect() as conn:
        exact = db.search_nodes(conn, 1, None, None, "E-500", 3)
        assert exact and exact[0]["node_id"] == pending_node
        from app.retrieval.tokenize import Tokenizer
        lex = db.search_nodes(conn, 1, Tokenizer().tokens("결제 캐시 서버 재시작"), None, None, 3)
        assert lex and lex[0]["node_id"] == pending_node
        # 다른 워크스페이스에서는 보이지 않는다
        assert not db.search_nodes(conn, 2, None, None, "E-500", 3)


async def test_failed_refine_increments_attempts(fake_mode, pending_node, admin_conn, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(worker.llm, "refine", boom)
    stats = await worker.run_once()
    assert stats["failed"] >= 1
    n = admin_conn.execute("SELECT refine_status, refine_attempts, refine_error, tokens FROM log_nodes WHERE id = %s", (pending_node,)).fetchone()
    assert n["refine_status"] == "pending" and n["refine_attempts"] == 1
    assert "llm down" in n["refine_error"]
    assert n["tokens"], "정제가 실패해도 토큰은 남겨 어휘 검색은 되게 한다"
    admin_conn.execute("UPDATE log_nodes SET refine_attempts = 2 WHERE id = %s", (pending_node,))
    await worker.run_once()
    assert admin_conn.execute("SELECT refine_status FROM log_nodes WHERE id = %s", (pending_node,)).fetchone()["refine_status"] == "failed"
