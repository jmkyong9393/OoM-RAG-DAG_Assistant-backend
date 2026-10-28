"""data/seed/*.json → PostgreSQL 적재. 멱등(ON CONFLICT DO NOTHING). --reset 이면 업무 테이블을 비우고 다시 넣는다.

data/seed/*.json 의 파일 9종과 필드가 **데이터 교체 인터페이스**다. 지금은 프론트 mocks 역수입(import_frontend_mocks.py)이 쓰지만,
나중에 LLM 으로 분석·생성한 합성 데이터 생성기가 같은 형식의 파일을 쓰면 이 스크립트와 DB·API 는 그대로다.
적재 전에 도메인 규칙(R2·R12 등)을 점검해 **경고로 보고**한다 — 임시 데이터는 통과시키되, 생성기 품질 기준으로 쓴다.

사용:  uv run --with "psycopg[binary]" --with bcrypt scripts/seed.py [--reset] [--strict]   (--strict: 규칙 위반이 있으면 적재하지 않음)
       SEED_PASSWORD: 시드 사용자 전원의 초기 비밀번호 (기본 Oom!demo-2026 — 작성규칙 충족, 로컬 전용)
환경:  DATABASE_URL (기본 postgresql://oom:oom@localhost:5432/oom; make seed 는 deploy/.env 를 읽는다)  — 스키마 소유 계정(oom_api 가 아님: 시드는 explicit id 로 넣는다)
"""
import json, os, sys, pathlib
import bcrypt
import psycopg
from psycopg.types.json import Jsonb

SEED_PASSWORD = os.environ.get("SEED_PASSWORD", "Oom!demo-2026")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
DSN = os.environ.get("DATABASE_URL", "postgresql://oom:oom@localhost:5432/oom")
RESET = "--reset" in sys.argv
STRICT = "--strict" in sys.argv

def load(name): return json.loads((SEED / name).read_text(encoding="utf-8"))

TABLES_IN_ORDER = ["tenants", "workspaces", "users", "workspace_members", "log_nodes", "log_edges",
                   "policy_documents", "policy_chunks", "audit_log"]


def validate() -> list[str]:
    """도메인 규칙 점검 (API 계층이 강제하는 것과 같은 규칙. 시드는 경고, 생성기는 이걸 0건으로 맞춘다)."""
    nodes = {n["id"]: n for n in load("log_nodes.json")}
    edges = load("log_edges.json")
    ws_ids = {w["id"] for w in load("workspaces.json")}
    members = {(m["workspace_id"], m["user_id"]) for m in load("workspace_members.json")}
    users = {u["id"] for u in load("users.json")}
    children = {e["child_id"] for e in edges}
    parents_of = {}
    for e in edges:
        parents_of.setdefault(e["child_id"], []).append(e["parent_id"])
    problems = []
    for n in nodes.values():
        if n["workspace_id"] not in ws_ids:
            problems.append(f"node {n['id']}: workspace {n['workspace_id']} 없음")
        if n["author_id"] not in users:
            problems.append(f"node {n['id']}: author {n['author_id']} 없음")
        elif (n["workspace_id"], n["author_id"]) not in members:
            problems.append(f"node {n['id']}: author {n['author_id']} 는 workspace {n['workspace_id']} 멤버가 아님")
        if n["id"] not in children and n["type"] == "success":
            problems.append(f"node {n['id']} ({n['title']!r}): R2 위반 — success 가 루트(상위 fail/ref 없음)")
        if n["type"] == "success":
            for p in parents_of.get(n["id"], []):
                if nodes.get(p, {}).get("type") == "success":
                    problems.append(f"node {n['id']}: R12 위반 — success 의 부모 {p} 도 success")
        if not (n.get("body") or "").strip():
            problems.append(f"node {n['id']}: body 비어 있음 (정제·검색 품질 저하)")
    for e in edges:
        a, b = nodes.get(e["parent_id"]), nodes.get(e["child_id"])
        if not a or not b:
            problems.append(f"edge {e['parent_id']}→{e['child_id']}: 노드 없음")
        elif a["workspace_id"] != b["workspace_id"]:
            problems.append(f"edge {e['parent_id']}→{e['child_id']}: 워크스페이스가 다름")
    return problems


def main():
    problems = validate()
    for p in problems:
        print("WARN", p)
    if problems:
        print(f"규칙 위반 {len(problems)}건 — 임시 데이터로 간주하고 적재" + (" 중단(--strict)" if STRICT else " 진행"))
        if STRICT:
            sys.exit(1)
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        if RESET:
            cur.execute("TRUNCATE " + ", ".join(reversed(TABLES_IN_ORDER)) + ", search_sessions, answer_feedback, workspace_terms, eval_runs RESTART IDENTITY CASCADE")
        for t in load("tenants.json"):
            cur.execute("INSERT INTO tenants (id, slug, name, settings) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (t["id"], t["slug"], t["name"], Jsonb(t.get("settings", {}))))
        for w in load("workspaces.json"):
            cur.execute("INSERT INTO workspaces (id, tenant_id, parent_id, kind, name, policy_enabled, settings) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (w["id"], w["tenant_id"], w["parent_id"], w["kind"], w["name"], w["policy_enabled"], Jsonb(w.get("settings", {}))))
        # 비밀번호: SEED_PASSWORD 하나를 전원에 BCrypt 로 넣는다(로컬 시연용). 이미 있는 사용자는 비밀번호를 건드리지 않는다.
        pw_hash = bcrypt.hashpw(SEED_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")
        for u in load("users.json"):
            cur.execute("""INSERT INTO users (id, tenant_id, name, title, employee_no, email, password_hash, password_changed_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                           ON CONFLICT (id) DO UPDATE SET employee_no = EXCLUDED.employee_no, email = EXCLUDED.email,
                             password_hash = coalesce(users.password_hash, EXCLUDED.password_hash),
                             password_changed_at = coalesce(users.password_changed_at, now())""",
                        (u["id"], u["tenant_id"], u["name"], u.get("title"), u.get("employee_no"), u.get("email"), pw_hash))
        for m in load("workspace_members.json"):
            cur.execute("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (m["workspace_id"], m["user_id"], m["role"]))
        for n in load("log_nodes.json"):
            cur.execute("""INSERT INTO log_nodes (id, workspace_id, type, status, title, body, author_id, error_code, keywords, resolution_summary,
                             refine_status, last_confirmed_at, deprecated_via, deprecate_reason, resolved_at, resolve_note, source, source_ref, created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                        (n["id"], n["workspace_id"], n["type"], n["status"], n["title"], n["body"], n["author_id"], n.get("error_code"),
                         n.get("keywords", []), n.get("resolution_summary"), n.get("refine_status", "pending"), n.get("last_confirmed_at"),
                         n.get("deprecated_via"), n.get("deprecate_reason"), n.get("resolved_at"), n.get("resolve_note"),
                         n.get("source", "synthetic"), n.get("source_ref"), n["created_at"]))
        for e in load("log_edges.json"):
            cur.execute("INSERT INTO log_edges (workspace_id, parent_id, child_id, created_by, origin) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (e["workspace_id"], e["parent_id"], e["child_id"], e.get("created_by"), e.get("origin", "manual")))
        for d in load("policy_documents.json"):
            cur.execute("""INSERT INTO policy_documents (id, workspace_id, slug, title, version, doc_type, source_type, file_path, status, retired_at,
                             retire_reason, retired_by, uploaded_by, uploaded_at, indexed_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s::timestamptz, now()),%s) ON CONFLICT (id) DO NOTHING""",
                        (d["id"], d["workspace_id"], d["slug"], d["title"], d["version"], d.get("doc_type"), d["source_type"], d.get("file_path"),
                         d["status"], d.get("retired_at"), d.get("retire_reason"), d.get("retired_by"), d.get("uploaded_by") or "script",
                         d.get("uploaded_at"), d.get("indexed_at")))
        for c in load("policy_chunks.json"):
            cur.execute("INSERT INTO policy_chunks (id, doc_id, workspace_id, article_label, page, text, bboxes) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (c["id"], c["doc_id"], c.get("workspace_id"), c["article_label"], c.get("page"), c["text"], Jsonb(c["bboxes"]) if c.get("bboxes") else None))
        for a in load("audit_log.json"):
            cur.execute("""INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label, at)
                           SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS (
                             SELECT 1 FROM audit_log WHERE table_name=%s AND row_id=%s AND action=%s AND at=%s::timestamptz)""",
                        (a["tenant_id"], a["workspace_id"], a["table_name"], a["row_id"], a["action"], a["actor"], a["source"], a.get("reason"), a.get("label"), a["at"],
                         a["table_name"], a["row_id"], a["action"], a["at"]))
        # explicit id 로 넣었으므로 시퀀스를 현재 최대값 뒤로
        for t in ["tenants", "workspaces", "users", "log_nodes", "policy_documents"]:
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), coalesce((SELECT max(id) FROM {t}), 0) + 1, false)")
        conn.commit()
        cur.execute("SELECT (SELECT count(*) FROM workspaces), (SELECT count(*) FROM users), (SELECT count(*) FROM workspace_members), "
                    "(SELECT count(*) FROM log_nodes), (SELECT count(*) FROM log_edges), (SELECT count(*) FROM policy_documents), "
                    "(SELECT count(*) FROM policy_chunks), (SELECT count(*) FROM audit_log)")
        print("workspaces, users, members, nodes, edges, docs, chunks, audit =", cur.fetchone())

if __name__ == "__main__":
    main()
