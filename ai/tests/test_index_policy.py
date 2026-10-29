"""규정 인덱싱 스크립트(scripts/index_policy.py) 통합 테스트 — oom_ai 권한으로 적재·재인덱싱·스킵·R8 개정 폐기, 워커 백필 후 검색."""
import json
import sys
from pathlib import Path

import pytest
import psycopg
from psycopg.rows import dict_row

from tests.conftest import requires_db

pytestmark = requires_db
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import index_policy  # noqa: E402

SLUG = "test-refund-v9"


def _doc(text_suffix: str = "", supersedes: str | None = None, title: str = "[test] 환불규정") -> dict:
    d = {"slug": SLUG, "workspace_id": 3, "title": title, "version": "v9.0", "doc_type": "규정", "source_type": "json",
         "effective_from": "2026-11-01", "tags": ["test"],
         "chunks": [{"article_label": "제1조 (목적)", "text": "이 규정은 테스트 환불 절차를 정한다." + text_suffix},
                    {"id": f"{SLUG}-a7-1", "article_label": "제7조 (개봉 상품)", "page": 3, "text": "개봉한 신선식품은 단순 변심 환불이 불가하다."}]}
    if supersedes:
        d["supersedes"] = supersedes
    return d


@pytest.fixture
def ai_conn():
    # 스크립트 main() 은 문서마다 conn.transaction() 으로 커밋한다. 테스트는 다른 연결(admin·워커)에서 결과를 봐야 하므로 autocommit.
    with psycopg.connect(index_policy.DSN, row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.fixture
def cleanup(admin_conn):
    yield
    admin_conn.execute("DELETE FROM audit_log WHERE table_name = 'policy_documents' AND (label LIKE '[test]%%' OR label LIKE '환불규정 v3%%')")
    admin_conn.execute("DELETE FROM policy_documents WHERE slug LIKE 'test-%%'")   # 청크는 CASCADE
    admin_conn.execute("UPDATE policy_documents SET status = 'active', retired_at = NULL, retire_reason = NULL, retired_by = NULL WHERE slug = 'refund-policy-v3'")


def test_validate_rejects_missing_fields(tmp_path):
    bad = {"slug": "x", "workspace_id": 3, "title": "t", "version": "v1", "chunks": [{"article_label": "", "text": "a"}, {"text": "b"}]}
    errs = index_policy.validate(bad, tmp_path / "x.chunks.json")
    assert len(errs) == 2 and all("article_label" in e for e in errs)
    assert index_policy.validate(_doc(), tmp_path / "ok.json") == []


def test_index_then_skip_then_reindex_and_r8(ai_conn, admin_conn, cleanup):
    # 1) 신규 적재 — 같은 제목의 active 문서(없음)만 폐기 대상이므로 아무것도 안 폐기
    r = index_policy.index_document(ai_conn, _doc())
    assert r["action"] == "indexed" and r["chunks"] == 2 and r["retired"] == []
    doc = admin_conn.execute("SELECT * FROM policy_documents WHERE slug = %s", (SLUG,)).fetchone()
    assert doc["status"] == "active" and doc["uploaded_by"] == "script" and doc["source_hash"] and doc["indexed_at"] is None
    chunks = admin_conn.execute("SELECT id, workspace_id, tokens FROM policy_chunks WHERE doc_id = %s ORDER BY id", (doc["id"],)).fetchall()
    assert [c["id"] for c in chunks] == [f"{SLUG}-001", f"{SLUG}-a7-1"]       # id 생략 시 순번, 있으면 그대로
    assert all(c["workspace_id"] == 3 and c["tokens"] is None for c in chunks)   # 워크스페이스는 트리거가 채움, 토큰은 워커 몫
    assert admin_conn.execute("SELECT count(*) AS c FROM audit_log WHERE table_name='policy_documents' AND row_id=%s AND action='indexed'",
                              (str(doc["id"]),)).fetchone()["c"] == 1

    # 2) 같은 내용 재실행 → 스킵 (해시)
    assert index_policy.index_document(ai_conn, _doc())["action"] == "skipped"
    assert index_policy.index_document(ai_conn, _doc(), dry_run=True)["action"] == "skipped"

    # 3) 본문 변경 → 청크 교체, 문서 id 유지
    r = index_policy.index_document(ai_conn, _doc(text_suffix=" (개정)"))
    assert r["action"] == "reindexed"
    assert admin_conn.execute("SELECT id FROM policy_documents WHERE slug = %s", (SLUG,)).fetchone()["id"] == doc["id"]
    assert admin_conn.execute("SELECT text FROM policy_chunks WHERE id = %s", (f"{SLUG}-001",)).fetchone()["text"].endswith("(개정)")
    assert admin_conn.execute("SELECT count(*) AS c FROM policy_chunks WHERE doc_id = %s", (doc["id"],)).fetchone()["c"] == 2

    # 4) R8 — supersedes 로 시드 환불규정 v3 를 지목하면 그 문서가 retired 되고 검색에서 빠진다
    r = index_policy.index_document(ai_conn, _doc(text_suffix=" (v9.1)", supersedes="refund-policy-v3"))
    assert r["retired"] == ["refund-policy-v3"]
    old = admin_conn.execute("SELECT status, retire_reason, retired_by FROM policy_documents WHERE slug = 'refund-policy-v3'").fetchone()
    assert old == {"status": "retired", "retire_reason": "v9.0 개정", "retired_by": "script"}
    assert admin_conn.execute("SELECT count(*) AS c FROM audit_log WHERE table_name='policy_documents' AND action='superseded' AND reason='v9.0 개정'").fetchone()["c"] == 1



async def test_backfill_makes_new_policy_searchable_and_old_hidden(ai_conn, admin_conn, cleanup, fake_mode):
    index_policy.index_document(ai_conn, _doc(supersedes="refund-policy-v3"))
    from app import worker
    from app.retrieval import db
    from app.retrieval.tokenize import Tokenizer
    stats = await worker.run_once()
    assert stats["chunks"] >= 2
    with db.connect() as conn:
        hits = [h["id"] for h in db.search_chunks(conn, 3, Tokenizer().tokens("개봉 신선식품 환불"), None, 5)]
    assert f"{SLUG}-a7-1" in hits
    assert not any(h.startswith("refund-policy-v3") for h in hits), "retired 된 구버전은 검색에서 빠진다"
