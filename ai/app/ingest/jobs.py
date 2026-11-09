"""업로드 작업(워커 한 주기): converting → 변환 · 미리보기 → review, indexing → 검수한 MD 로 조문 적재 → approved.

한 건씩 FOR UPDATE SKIP LOCKED 로 집고, 처리하는 동안 행 잠금을 쥔 채 끝에 커밋한다(워커가 둘이어도 같은 업로드를 두 번 처리하지 않게).
임베딩 · 토큰은 같은 주기의 backfill_chunk_tokens 가 채운다(app.worker.run_once 순서).
"""
from __future__ import annotations

import types
from pathlib import Path

from app.config import uploads_dir
from app.ingest.convert import ConversionError, _scripts, ocr_suspects, pdf_page_texts, preview, to_markdown

SOURCE_TYPE = {"pdf": "pdf", "hwp": "hwp", "hwpx": "hwp"}   # 그 밖(docx · md · txt)은 markdown — policy_documents.source_type 허용값


def _claim(conn, status: str) -> dict | None:
    return conn.execute("""
        SELECT u.*, (SELECT name FROM users WHERE id = u.uploaded_by) AS uploader, (SELECT name FROM users WHERE id = u.reviewed_by) AS reviewer
        FROM policy_uploads u WHERE u.status = %s ORDER BY u.id LIMIT 1 FOR UPDATE OF u SKIP LOCKED""", (status,)).fetchone()


def convert_one(conn) -> str | None:
    """converting 1건: 변환본이 없으면 원본에서 추출, 있으면(검수자가 고침) 미리보기만 다시."""
    row = _claim(conn, "converting")
    if row is None:
        conn.rollback()
        return None
    from psycopg.types.json import Jsonb
    try:
        report = dict(row["report"] or {})
        if row["markdown"] is None:
            md, extracted = to_markdown(uploads_dir() / row["original_path"], row["format"])
            report["extract"] = extracted
        else:
            md = row["markdown"]
        report.update(preview(md))
        ocr_pages = (report.get("extract") or {}).get("ocr_pages") or []
        if ocr_pages:   # 스캔본: 검수자가 원본과 대조할 줄을 짚어 준다
            report["ocr_suspects"] = ocr_suspects(md)
            report["warnings"].insert(0, f"{len(ocr_pages)}쪽을 OCR 로 읽었습니다 — 원문자(①②) · 굵은 제목 · 일부 낱말이 틀릴 수 있어 "
                                         f"원본과 대조가 필요합니다(확인할 줄 {len(report['ocr_suspects'])}개)")
        conn.execute("UPDATE policy_uploads SET status = 'review', markdown = %s, report = %s, error = NULL, updated_at = now() WHERE id = %s",
                     (md, Jsonb(report), row["id"]))
        result = "review"
    except Exception as e:  # noqa: BLE001 — 도구 없음 · 깨진 파일 · 예상 못 한 오류 모두 실패로 남겨 다음 주기에 같은 건을 되풀이하지 않는다
        conn.rollback()
        msg = str(e) if isinstance(e, ConversionError) else f"{type(e).__name__}: {e}"
        conn.execute("UPDATE policy_uploads SET status = 'failed', error = %s, updated_at = now() WHERE id = %s", (msg[:500], row["id"]))
        result = "failed"
    conn.commit()
    return result


def index_one(conn) -> str | None:
    """indexing 1건: 검수한 MD 를 저장소에 보관하고 조문 JSON → policy_documents · policy_chunks. 실패하면 검수 상태로 되돌린다(고친 변환본은 남는다)."""
    row = _claim(conn, "indexing")
    if row is None:
        conn.rollback()
        return None
    _scripts()
    from build_policy_json import build
    from index_policy import index_document
    try:
        rel_md = str(Path(row["original_path"]).parent / "reviewed.md")
        md_path = uploads_dir() / rel_md
        md_path.write_text(row["markdown"], encoding="utf-8", newline="\n")
        args = types.SimpleNamespace(slug=row["slug"], workspace=row["workspace_id"], title=row["title"], version=row["version"],
                                     doc_type=row["doc_type"], source_type=SOURCE_TYPE.get(row["format"], "markdown"),
                                     file_path="uploads/" + row["original_path"], effective_from=None, tags=[], supersedes=row["supersedes"],
                                     max_chars=700, terms=None, author=row["uploader"], summary=None)
        doc = build(md_path, args)
        doc["meta"].update({"converter": "policy_uploads", "upload_id": row["id"], "original_name": row["original_name"], "sha256": row["sha256"]})
        report = dict(row["report"] or {})
        if row["format"] == "pdf":   # 원본 PDF 에서 조문마다 쪽(·글자층이 있으면 위치)을 찾아 근거 조문 '원문 보기'가 그 쪽을 연다
            from pdf_locate import locate
            orig = uploads_dir() / row["original_path"]
            report["located"] = locate(doc, pdf_page_texts(orig, (report.get("extract") or {}).get("ocr_pages") or []), orig)
        result = index_document(conn, doc)
        doc_id = conn.execute("SELECT id FROM policy_documents WHERE slug = %s", (row["slug"],)).fetchone()["id"]
        conn.execute("UPDATE policy_documents SET uploaded_by = %s, approved_by = %s, uploaded_at = now() WHERE id = %s",
                     (row["uploader"], row["reviewer"], doc_id))
        report["indexed"] = {"chunks": result["chunks"], "retired": result["retired"]}
        from psycopg.types.json import Jsonb
        conn.execute("""UPDATE policy_uploads SET status = 'approved', document_id = %s, markdown_path = %s, report = %s, error = NULL, updated_at = now()
                        WHERE id = %s""", (doc_id, rel_md, Jsonb(report), row["id"]))
        status = "approved"
    except Exception as e:  # noqa: BLE001 — 등록 실패는 검수로 되돌려 사람이 보게 한다
        conn.rollback()
        conn.execute("UPDATE policy_uploads SET status = 'review', error = %s, updated_at = now() WHERE id = %s",
                     (f"등록 실패: {type(e).__name__}: {e}"[:500], row["id"]))
        status = "review"
    conn.commit()
    return status


def run_pending(conn, limit: int = 20) -> dict:
    """대기 중인 업로드를 처리한다(한 주기 최대 limit 건). 변환은 OCR 로 오래 걸릴 수 있어 건수를 제한한다."""
    stats = {"converted": 0, "convert_failed": 0, "indexed": 0, "index_failed": 0}
    for _ in range(limit):
        r = convert_one(conn)
        if r is None:
            break
        stats["converted" if r == "review" else "convert_failed"] += 1
    for _ in range(limit):
        r = index_one(conn)
        if r is None:
            break
        stats["indexed" if r == "approved" else "index_failed"] += 1
    return stats
