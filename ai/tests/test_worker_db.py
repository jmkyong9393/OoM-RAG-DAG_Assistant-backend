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
    assert "E-500" in terms and "결제" not in terms, "에러 코드는 사전에 넣고, 이미 한 토큰인 일반 명사는 넣지 않는다"
    # 백필 결과: 시드 노드·청크에 tokens 가 모두 채워져 있다
    assert admin_conn.execute("SELECT count(*) AS c FROM policy_chunks WHERE tokens IS NULL OR title_tokens IS NULL").fetchone()["c"] == 0
    # 사용자 사전은 전 워크스페이스 합집합(kiwi 사용자 단어가 프로세스 전역이라 나눌 수 없다): 전사 공통(10)에 넣은 용어가 사전에 들어간다
    from app.retrieval import db as _db
    admin_conn.execute("INSERT INTO workspace_terms (workspace_id, term, source) VALUES (10, '서킷브레이커', 'manual') ON CONFLICT DO NOTHING")
    admin_conn.commit()
    try:
        assert "서킷브레이커" in _db.user_terms(admin_conn)
    finally:
        admin_conn.execute("DELETE FROM workspace_terms WHERE workspace_id = 10 AND term = '서킷브레이커'"); admin_conn.commit()
    # 제목 토큰은 D(기본, 표기 없음)·라벨+본문은 A 가중 — 문서 제목만의 토큰 '지침' 은 A 가 붙지 않는다 (V7)
    import re
    tsv = admin_conn.execute("SELECT tsv::text AS t, tokens, title_tokens FROM policy_chunks WHERE id = 'vpn-guide-v22-a15-1'").fetchone()
    assert "지침" in tsv["title_tokens"].split() and "지침" not in tsv["tokens"].split()
    assert re.search(r"'토큰':\d+A", tsv["t"]) and re.search(r"'지침':\d+(?![0-9A-C])", tsv["t"]), tsv["t"]
    assert admin_conn.execute("SELECT count(*) AS c FROM log_nodes WHERE tokens IS NULL AND refine_status <> 'pending'").fetchone()["c"] == 0
    # 두 번째 실행은 할 일이 없다
    again = await worker.run_once()
    assert again == {"refined": 0, "failed": 0, "chunks": 0, "nodes": 0, "converted": 0, "convert_failed": 0, "indexed": 0, "index_failed": 0}


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



def test_classify_context_reads_workspace_data():
    """db.classify_context — 사례 유무와 에러 코드의 규정 등장 여부. 시드: ws1·ws2 에 로그. 전사 공통(10)에는 로그를 두지 않는다."""
    from app.retrieval import db
    with db.connect() as conn:
        assert db.classify_context(conn, 1, None) == {"has_cases": True, "code_in_policy": False}
        assert db.classify_context(conn, 10, None)["has_cases"] is False
        assert db.classify_context(conn, 2, "E-NOSUCH-999")["code_in_policy"] is False
        row = conn.execute("""SELECT c.workspace_id FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
                              WHERE d.status = 'active' AND c.text LIKE '%%E-403%%' LIMIT 1""").fetchone()
        if row:   # make index 로 VPN 지침(제15·17조)이 적재된 상태에서만 확인
            assert db.classify_context(conn, row["workspace_id"], "E-403")["code_in_policy"] is True


def test_dictionary_terms_keep_only_words_the_analyzer_splits():
    from app.retrieval.tokenize import Tokenizer
    tok = Tokenizer()
    got = worker.dictionary_terms(tok, ["기한", "권한", "네이티브 영역", "90일", "봇", "서킷브레이커", " "], "E-500")
    assert "기한" not in got and "권한" not in got, "이미 한 토큰인 일반 명사"
    assert "네이티브 영역" not in got and "90일" not in got and "봇" not in got
    assert got[-1] == "E-500"
    if tok.backend == "kiwi":
        assert ("서킷브레이커" in got) == (tok.tokens("서킷브레이커").split() != ["서킷브레이커"])


def test_embedding_mismatch_detects_vectors_from_another_model(pending_node, admin_conn):
    from app.retrieval import db as _db
    vec = "[" + ",".join(["0"] * 1535 + ["1"]) + "]"
    admin_conn.execute("UPDATE log_nodes SET embedding = %s::vector, embedding_model = 'zz-old' WHERE id = %s", (vec, pending_node))
    admin_conn.commit()
    with _db.connect() as conn:
        assert _db.embedding_mismatch(conn, "zz-new") is True
