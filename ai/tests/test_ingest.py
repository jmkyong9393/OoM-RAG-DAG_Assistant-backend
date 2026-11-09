"""규정 업로드 변환 · 등록(app.ingest). 형식별 추출은 도구가 있을 때만(tesseract · pandoc), DB 작업은 로컬 Postgres 가 있을 때만."""
from __future__ import annotations

import shutil
import uuid

import pytest

from app.ingest import convert, jobs
from tests.conftest import requires_db

MD = "# 시험 규정\n\n제1조(목적) 이 규정은 업로드 시험용이다.\n\n제2조(적용) 모든 직원에게 적용한다.\n\n부칙\n\n제1조(시행일) 이 규정은 공포한 날부터 시행한다.\n"


def test_normalize_fixes_pandoc_escapes_and_addenda():
    out = convert.normalize("\\[별표 1\\] 등급\n부 칙\n제1조(시행일) 즉시\n")
    assert "[별표 1] 등급" in out and "## 부칙" in out


def test_hwp_para_text_skips_inline_controls():
    # 0x0B(확장 제어)는 8 글자 묶음 — 묶음 안의 제어 id 가 한자처럼 섞이면 안 된다
    raw = (chr(0x0B) + "dces" + "\x00\x00" + chr(0x0B)).encode("utf-16-le") + "제1조 목적".encode("utf-16-le") + b"\r\x00"
    assert convert.para_text(raw) == "제1조 목적\n"


def test_markdown_preview_counts_articles_and_keeps_addenda_apart(tmp_path):
    p = tmp_path / "rule.md"
    p.write_text(MD, encoding="utf-8")
    md, report = convert.to_markdown(p, "md")
    pv = convert.preview(md)
    labels = [a["label"] for a in pv["articles"] if a["article"]]
    assert pv["article_count"] == 3 and any(l.startswith("부칙") for l in labels)
    assert not any("두 번" in w for w in pv["warnings"])   # 부칙 제1조가 본문 제1조와 겹치지 않는다


def test_preview_warns_when_no_articles():
    pv = convert.preview("그냥 메모입니다.\n")
    assert pv["article_count"] == 0 and pv["warnings"]


def test_ocr_suspects_flag_real_misreads_but_not_clean_lines():
    # 실측 OCR 결과(300dpi 스캔)에서 가져온 줄: 앞 6줄은 오인식, 뒤 4줄은 바르게 읽힌 줄
    md = "\n".join([
        "=ss     으     ^",
        "제1조(목적) 이 FAS 임직원의 자율적인 동호회 활동을 장려하고",
        "제3조(등록) © 동호회를 결성하려는 임직원은",
        "2) 경영지원 부서는 신청일부터 14일 이내에 등록 여부를 신청자에게 알려야 한다.",
        "@ 한 동호회에 지원하는 활동비는 월 60만 원을 넘을 수 없다.",
        "제4조(활동비 지원) 0 SHE 동호회에는 회원 1인당 월 2만 원의 활동비를 지원한다.",
        "1. 주류만을 구매하는 비용",
        "10명 ~19명 | 회원 수 ×2만 원",
        "제7조(해산) 회원이 10명 미만인 상태가 3개월 이상 계속되거나",
        "② 정산서를 기한 안에 제출하지 않은 동호회에는 다음 분기 활동비 지급을 보류한다.",
    ])
    assert [s["line"] for s in convert.ocr_suspects(md)] == [1, 2, 3, 4, 5, 6]


def _pdf(path, lines):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((50, 72 + 20 * i), line, fontname="korea", fontsize=11)
    doc.save(path)
    return path


def test_text_pdf_is_read_without_ocr(tmp_path):
    p = _pdf(tmp_path / "rule.pdf", ["제1조(목적) 이 규정은 시험용이다.", "제2조(적용) 모든 직원에게 적용한다."])
    md, report = convert.to_markdown(p, "pdf")
    assert report["ocr_pages"] == [] and convert.preview(md)["article_count"] == 2


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract 없음(운영 이미지에는 있음)")
def test_scanned_pdf_goes_through_ocr(tmp_path):
    import pymupdf
    src = pymupdf.open(_pdf(tmp_path / "text.pdf", ["제1조(목적) 이 규정은 시험용이다.", "제2조(적용) 모든 직원에게 적용한다."]))
    scan = pymupdf.open()
    pix = src[0].get_pixmap(dpi=300)
    page = scan.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    scan.save(tmp_path / "scan.pdf")
    md, report = convert.to_markdown(tmp_path / "scan.pdf", "pdf")
    assert report["ocr_pages"] == [1] and convert.preview(md)["article_count"] == 2


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc 없음(운영 이미지에는 있음)")
def test_docx_keeps_appendix_label(tmp_path):
    import subprocess
    (tmp_path / "rule.md").write_text(MD + "\n[별표 1] 등급표\n\n| 등급 | 기간 |\n|---|---|\n| 일반 | 30일 |\n", encoding="utf-8")
    subprocess.run(["pandoc", str(tmp_path / "rule.md"), "-o", str(tmp_path / "rule.docx")], check=True)
    md, _ = convert.to_markdown(tmp_path / "rule.docx", "docx")
    labels = [a["label"] for a in convert.preview(md)["articles"]]
    assert any(l.startswith("[별표 1]") for l in labels)


def test_unsupported_or_empty_fails_clearly(tmp_path):
    (tmp_path / "empty.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(convert.ConversionError):
        convert.to_markdown(tmp_path / "empty.md", "md")
    with pytest.raises(convert.ConversionError):
        convert.to_markdown(tmp_path / "x.rtf", "rtf")


@requires_db
def test_upload_job_converts_then_indexes_as_document(tmp_path, monkeypatch, admin_conn):
    """converting → review(미리보기) → indexing → approved: 검수한 MD 로 문서 · 조문이 생기고 승인자가 남는다."""
    from app.retrieval import db
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    tag = uuid.uuid4().hex[:8]
    slug, title = f"test-upload-{tag}", f"업로드 시험 규정 {tag}"
    (tmp_path / tag).mkdir()
    (tmp_path / tag / "original.md").write_text(MD, encoding="utf-8")
    uid = admin_conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    up = admin_conn.execute("""INSERT INTO policy_uploads (workspace_id, original_name, format, size_bytes, sha256, original_path, slug, title, version, uploaded_by)
                               VALUES (2, '시험.md', 'md', %s, 'x', %s, %s, %s, '2026-11', %s) RETURNING id""",
                            (len(MD), f"{tag}/original.md", slug, title, uid)).fetchone()["id"]
    try:
        with db.connect() as conn:
            assert jobs.convert_one(conn) == "review"
        row = admin_conn.execute("SELECT status, report, markdown FROM policy_uploads WHERE id = %s", (up,)).fetchone()
        assert row["status"] == "review" and row["report"]["article_count"] == 3 and row["report"]["extract"]["format"] == "md"
        admin_conn.execute("UPDATE policy_uploads SET status = 'indexing', reviewed_by = %s WHERE id = %s", (uid, up))
        with db.connect() as conn:
            assert jobs.index_one(conn) == "approved"
        row = admin_conn.execute("SELECT status, document_id, markdown_path FROM policy_uploads WHERE id = %s", (up,)).fetchone()
        d = admin_conn.execute("SELECT status, approved_by, uploaded_by, file_path, (SELECT count(*) FROM policy_chunks c WHERE c.doc_id = d.id) AS n "
                               "FROM policy_documents d WHERE id = %s", (row["document_id"],)).fetchone()
        assert row["status"] == "approved" and (tmp_path / row["markdown_path"]).read_text(encoding="utf-8") == convert.normalize(MD)
        assert d["status"] == "active" and d["approved_by"] and d["uploaded_by"] != "script" and d["n"] == 3
        assert d["file_path"] == f"uploads/{tag}/original.md"
    finally:
        doc = admin_conn.execute("SELECT id FROM policy_documents WHERE slug = %s", (slug,)).fetchone()
        admin_conn.execute("DELETE FROM policy_uploads WHERE id = %s", (up,))
        if doc:
            admin_conn.execute("DELETE FROM policy_chunks WHERE doc_id = %s", (doc["id"],))
            admin_conn.execute("DELETE FROM policy_documents WHERE id = %s", (doc["id"],))
        admin_conn.execute("DELETE FROM audit_log WHERE table_name = 'policy_documents' AND label LIKE %s", (f"%{tag}%",))


@requires_db
def test_pdf_upload_records_page_of_each_article(tmp_path, monkeypatch, admin_conn):
    """원본 PDF 에서 조문의 쪽을 찾아 청크에 남긴다(근거 조문 '원문 보기' 가 그 쪽을 연다)."""
    import pymupdf
    from app.retrieval import db
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    tag = uuid.uuid4().hex[:8]
    slug = f"test-upload-pdf-{tag}"
    (tmp_path / tag).mkdir()
    pdf = pymupdf.open()
    for line in ["제1조(목적) 이 규정은 쪽 번호 시험용이다.", "제2조(적용) 모든 직원에게 적용한다."]:
        pdf.new_page().insert_text((50, 72), line, fontname="korea", fontsize=11)
    pdf.save(tmp_path / tag / "original.pdf")
    uid = admin_conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    up = admin_conn.execute("""INSERT INTO policy_uploads (workspace_id, original_name, format, size_bytes, sha256, original_path, slug, title, version, uploaded_by)
                               VALUES (2, '쪽.pdf', 'pdf', 1, 'x', %s, %s, %s, '1', %s) RETURNING id""",
                            (f"{tag}/original.pdf", slug, f"쪽 시험 {tag}", uid)).fetchone()["id"]
    try:
        with db.connect() as conn:
            assert jobs.convert_one(conn) == "review"
        assert (tmp_path / tag / "pages.json").exists()   # 쪽별 글자는 보고가 아니라 파일로
        assert "page_texts" not in admin_conn.execute("SELECT report FROM policy_uploads WHERE id = %s", (up,)).fetchone()["report"]["extract"]
        monkeypatch.setattr(jobs, "pdf_page_texts", lambda *a: pytest.fail("등록 때 원본을 다시 읽으면 안 된다(스캔본은 OCR 을 또 돈다)"))
        admin_conn.execute("UPDATE policy_uploads SET status = 'indexing', reviewed_by = %s WHERE id = %s", (uid, up))
        with db.connect() as conn:
            assert jobs.index_one(conn) == "approved"
        row = admin_conn.execute("SELECT document_id, report FROM policy_uploads WHERE id = %s", (up,)).fetchone()
        pages = [r["page"] for r in admin_conn.execute("SELECT page FROM policy_chunks WHERE doc_id = %s ORDER BY id", (row["document_id"],))]
        assert pages == [1, 2] and row["report"]["located"]["missing"] == []
        ev = admin_conn.execute("SELECT actor, source FROM audit_log WHERE table_name = 'policy_documents' AND row_id = %s AND action = 'indexed'",
                                (str(row["document_id"]),)).fetchone()
        reviewer = admin_conn.execute("SELECT name FROM users WHERE id = %s", (uid,)).fetchone()["name"]
        assert ev["actor"] == reviewer and ev["source"] == "ui"   # 문서 이력에 승인자가 남는다(스크립트 적재는 'script')
    finally:
        doc = admin_conn.execute("SELECT id FROM policy_documents WHERE slug = %s", (slug,)).fetchone()
        admin_conn.execute("DELETE FROM policy_uploads WHERE id = %s", (up,))
        if doc:
            admin_conn.execute("DELETE FROM policy_chunks WHERE doc_id = %s", (doc["id"],))
            admin_conn.execute("DELETE FROM policy_documents WHERE id = %s", (doc["id"],))
            admin_conn.execute("DELETE FROM audit_log WHERE table_name = 'policy_documents' AND row_id = %s", (str(doc["id"]),))


@requires_db
def test_broken_file_is_marked_failed_not_retried_forever(tmp_path, monkeypatch, admin_conn):
    from app.retrieval import db
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    tag = uuid.uuid4().hex[:8]
    (tmp_path / tag).mkdir()
    (tmp_path / tag / "original.hwp").write_bytes(b"not an ole file")
    uid = admin_conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    up = admin_conn.execute("""INSERT INTO policy_uploads (workspace_id, original_name, format, size_bytes, sha256, original_path, slug, title, version, uploaded_by)
                               VALUES (2, '깨짐.hwp', 'hwp', 15, 'x', %s, %s, '깨진 규정', '1', %s) RETURNING id""",
                            (f"{tag}/original.hwp", f"test-broken-{tag}", uid)).fetchone()["id"]
    try:
        with db.connect() as conn:
            assert jobs.convert_one(conn) == "failed"
        row = admin_conn.execute("SELECT status, error FROM policy_uploads WHERE id = %s", (up,)).fetchone()
        assert row["status"] == "failed" and "HWP 5" in row["error"]
    finally:
        admin_conn.execute("DELETE FROM policy_uploads WHERE id = %s", (up,))
