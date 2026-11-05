"""규정 문서 인덱싱: data/policies/*.chunks.json → policy_documents + policy_chunks (oom_ai 권한).

규칙
  - source_hash(제목·버전·청크 본문의 SHA-256)가 같은 문서는 스킵 (재실행 멱등)
  - 같은 slug 가 있는데 해시가 다르면 청크를 교체하고 문서 메타를 갱신 (재인덱싱)
  - R8 개정 자동 폐기: `supersedes` 로 지목한 slug, 또는 같은 workspace·같은 title 의 다른 active 문서를 retired 로
    (retire_reason='<version> 개정', retired_by='script', audit_log superseded)
  - tokens/embedding 은 비워 두고 정제 워커가 채운다: `python -m app.worker --once` (make index 가 이어서 실행)
  - `terms`(meta.json → build_policy_json.py --meta)는 workspace_terms(workspace_id, term, source='manual') 에 넣는다 — kiwi 사용자 사전.
    사전은 전 워크스페이스 합집합 하나(db.user_terms)이므로 새 term 이 들어가면 `app.worker --once --reindex` 로 기존 청크·노드도 재토크나이즈해야 한다

파일 형식 (data/policies/README.md):
  {"slug", "workspace_id", "title", "version", "doc_type", "source_type", "file_path"?, "effective_from"?, "supersedes"?, "tags"?, "terms"?, "meta"?,
   "chunks": [{"id"?, "article_label", "article_title"?, "page"?, "text", "bboxes"?}]}

실행: uv run --with "psycopg[binary]" scripts/index_policy.py [파일...] [--dry-run]   (파일 생략 시 data/policies/*.chunks.json 전부)
환경: AI_DB_URL (기본 postgresql://oom_ai:oom_ai@localhost:5432/oom)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("AI_DB_URL", "postgresql://oom_ai:oom_ai@localhost:5432/oom")


def source_hash(doc: dict) -> str:
    canon = json.dumps({"title": doc["title"], "version": doc["version"],
                        "chunks": [[c.get("id"), c["article_label"], c.get("page"), c["text"]] for c in doc["chunks"]]},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def chunk_id(slug: str, c: dict, seq: int) -> str:
    return c.get("id") or f"{slug}-{seq:03d}"


def validate(doc: dict, path: Path) -> list[str]:
    errs = []
    for k in ("slug", "workspace_id", "title", "version", "chunks"):
        if k not in doc:
            errs.append(f"{path.name}: '{k}' 없음")
    if doc.get("source_type", "json") not in ("pdf", "hwp", "markdown", "json"):
        errs.append(f"{path.name}: source_type 은 pdf|hwp|markdown|json")
    for i, c in enumerate(doc.get("chunks", [])):
        if not c.get("article_label") or not c.get("text"):
            errs.append(f"{path.name}: chunks[{i}] article_label·text 필수")
    ids = [chunk_id(doc.get("slug", ""), c, i + 1) for i, c in enumerate(doc.get("chunks", []))]
    if len(ids) != len(set(ids)):
        errs.append(f"{path.name}: 청크 id 중복")
    return errs


def add_terms(conn, ws: int, terms: list[str]) -> int:
    """meta.json terms → workspace_terms(manual). 이미 있으면(auto 포함) 그대로. 넣은 건수를 돌려준다."""
    n = 0
    for t in dict.fromkeys(x.strip() for x in terms if x and x.strip()):
        n += conn.execute("INSERT INTO workspace_terms (workspace_id, term, source) VALUES (%s, %s, 'manual') ON CONFLICT DO NOTHING", (ws, t)).rowcount
    return n


def index_document(conn, doc: dict, dry_run: bool = False) -> dict:
    slug, ws = doc["slug"], doc["workspace_id"]
    h = source_hash(doc)
    existing = conn.execute("SELECT id, source_hash, status FROM policy_documents WHERE slug = %s", (slug,)).fetchone()
    result = {"slug": slug, "action": None, "chunks": len(doc["chunks"]), "retired": [], "terms": 0}
    if not dry_run:
        result["terms"] = add_terms(conn, ws, doc.get("terms") or [])   # 문서가 스킵돼도 terms 는 멱등 적재 (meta.json 에 term 만 추가한 경우)
    if existing and existing["source_hash"] == h:
        result["action"] = "skipped"
        return result
    if dry_run:
        result["action"] = "would_" + ("reindex" if existing else "index")
        return result

    now = datetime.now(timezone.utc)
    if existing:
        doc_id = existing["id"]
        conn.execute("""UPDATE policy_documents SET title=%s, version=%s, doc_type=%s, tags=%s, source_type=%s, file_path=%s, source_hash=%s,
                          effective_from=%s, meta=%s, indexed_at=NULL WHERE id=%s""",
                     (doc["title"], doc["version"], doc.get("doc_type"), doc.get("tags", []), doc.get("source_type", "json"), doc.get("file_path"),
                      h, doc.get("effective_from"), Jsonb(doc.get("meta", {})), doc_id))
        conn.execute("DELETE FROM policy_chunks WHERE doc_id = %s", (doc_id,))
        result["action"] = "reindexed"
    else:
        doc_id = conn.execute("""INSERT INTO policy_documents (workspace_id, slug, title, version, doc_type, tags, source_type, file_path, source_hash,
                                   effective_from, meta, uploaded_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'script') RETURNING id""",
                              (ws, slug, doc["title"], doc["version"], doc.get("doc_type"), doc.get("tags", []), doc.get("source_type", "json"),
                               doc.get("file_path"), h, doc.get("effective_from"), Jsonb(doc.get("meta", {})))).fetchone()["id"]
        result["action"] = "indexed"
    for i, c in enumerate(doc["chunks"]):
        conn.execute("""INSERT INTO policy_chunks (id, doc_id, workspace_id, article_label, article_title, page, text, bboxes, meta)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (chunk_id(slug, c, i + 1), doc_id, ws, c["article_label"], c.get("article_title"), c.get("page"), c["text"],
                      Jsonb(c["bboxes"]) if c.get("bboxes") else None, Jsonb(c.get("meta", {}))))

    # R8: 개정 자동 폐기
    if doc.get("supersedes"):
        olds = conn.execute("SELECT id, slug, version FROM policy_documents WHERE slug = %s AND status = 'active' AND id <> %s",
                            (doc["supersedes"], doc_id)).fetchall()
    else:
        olds = conn.execute("""SELECT id, slug, version FROM policy_documents
                               WHERE workspace_id = %s AND title = %s AND status = 'active' AND id <> %s""", (ws, doc["title"], doc_id)).fetchall()
    for o in olds:
        reason = f"{doc['version']} 개정"
        conn.execute("UPDATE policy_documents SET status='retired', retired_at=%s, retire_reason=%s, retired_by='script' WHERE id=%s", (now, reason, o["id"]))
        conn.execute("""INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label)
                        SELECT w.tenant_id, %s, 'policy_documents', %s, 'superseded', 'script', 'script', %s, %s FROM workspaces w WHERE w.id = %s""",
                     (ws, str(o["id"]), reason, f"{doc['title']} {o['version']}", ws))
        result["retired"].append(o["slug"])
    conn.execute("""INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label)
                    SELECT w.tenant_id, %s, 'policy_documents', %s, 'indexed', 'script', 'script', %s, %s FROM workspaces w WHERE w.id = %s""",
                 (ws, str(doc_id), f"chunks={len(doc['chunks'])}", f"{doc['title']} {doc['version']}", ws))
    return result


def order_by_supersedes(docs: list[dict]) -> list[dict]:
    """개정 사슬 순서로 정렬 — 대체되는 문서를 대체하는 문서보다 먼저 적재한다.
    파일명 순으로 넣으면 새 문서가 먼저 들어간 뒤 옛 문서가 active 로 적재돼, 같은 제목의 문서 둘이 함께 살아남는다."""
    by = {d["slug"]: d for d in docs}
    out: list[dict] = []
    seen: set[str] = set()

    def visit(d: dict) -> None:
        if d["slug"] in seen:
            return
        seen.add(d["slug"])
        old = by.get(d.get("supersedes"))
        if old is not None:
            visit(old)
        out.append(d)

    for d in docs:
        visit(d)
    return out


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")] or sorted((ROOT / "data" / "policies").glob("*.chunks.json"))
    if not paths:
        print("인덱싱할 파일이 없습니다 (data/policies/*.chunks.json)")
        return 1
    docs = []
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        errs = validate(doc, p)
        if errs:
            print("\n".join("ERROR " + e for e in errs))
            return 1
        docs.append(doc)
    docs = order_by_supersedes(docs)
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        for doc in docs:
            with conn.transaction():
                r = index_document(conn, doc, dry)
            print(f"{r['action']:<14} {r['slug']:<28} chunks={r['chunks']}" + (f" retired={r['retired']}" if r["retired"] else "")
                  + (f" terms+{r['terms']}" if r.get("terms") else ""))
    if not dry:
        print("다음: cd ai && uv run python -m app.worker --once --reindex  (토큰·임베딩 백필. terms 가 늘었으면 --reindex 로 기존 청크·노드 재토크나이즈)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
