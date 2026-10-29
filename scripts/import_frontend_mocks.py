"""프론트 목업(mocks/*.json)을 백엔드 data/seed/*.json(단일 원천)으로 1회 역수입한다.
이후에는 data/ 가 원천이고 프론트 mocks 는 export_mocks.py 로 내보낸 사본이다 (개발문서 33 §5-2).

사용: python scripts/import_frontend_mocks.py <프론트 레포 경로>
"""
import json, sys, pathlib

FE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "../OoM-RAG-DAG_Assistant")
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "seed"
OUT.mkdir(parents=True, exist_ok=True)

def load(name): return json.loads((FE / "mocks" / name).read_text(encoding="utf-8"))
def dump(name, obj): (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

ws_fe, users_fe = load("workspaces.json"), load("users.json")
logs_fe, edges_fe = load("logs.json"), load("edges.json")
docs_fe, chunks_fe, events_fe = load("documents.json"), load("chunks.json"), load("doc-events.json")

# 조직: 테넌트 1 + 회사 루트(id 10, 규정 공통) + 부서 3(프론트 id 1·2·3 유지 — URL /w/{id} 호환)
tenant = {"id": 1, "slug": "oom-demo", "name": "OoM 시연 회사", "settings": {"llm_profile": "default", "employee_prefix": "OOM"}}
workspaces = [{"id": 10, "tenant_id": 1, "parent_id": None, "kind": "company", "name": "전사 공통", "policy_enabled": True,
               "settings": {"language": "ko"}}]
for w in ws_fe:
    workspaces.append({"id": w["id"], "tenant_id": 1, "parent_id": 10, "kind": "dept", "name": w["name"],
                       "policy_enabled": w["policy_enabled"], "settings": {"language": "ko"}})

# 사용자: 홈 부서 admin, 나머지 부서 member (데모에서 어느 워크스페이스든 들어갈 수 있게)
# 로그인은 사번+비밀번호(ADR 안건 G). 사번 = 회사 코드(tenant settings.employee_prefix) + 4자리. 이메일은 연락처.
# 비밀번호는 seed.py 가 SEED_PASSWORD 로 해시해 넣는다.
PREFIX = "OOM"
EMAILS = {1: "bae@oom.local", 2: "jang@oom.local", 3: "shin@oom.local", 4: "hong@oom.local", 5: "kim@oom.local"}
users = [{"id": u["id"], "tenant_id": 1, "name": u["name"], "title": u.get("title"), "employee_no": f"{PREFIX}{u['id']:04d}",
          "email": EMAILS.get(u["id"], f"user{u['id']}@oom.local"), "home_workspace_id": u["workspace_id"]} for u in users_fe]
members = []
for u in users:
    for w in ws_fe:
        members.append({"workspace_id": w["id"], "user_id": u["id"], "role": "admin" if w["id"] == u["home_workspace_id"] else "member"})
# 격리 시연용: 한 부서(CS)에만 속한 사용자. 이 계정으로 다른 워크스페이스를 열면 403 (프론트 mocks 에는 없음)
users.append({"id": 5, "tenant_id": 1, "name": "김하늘", "title": "상담사", "employee_no": f"{PREFIX}0005", "email": EMAILS[5], "home_workspace_id": 3})
members.append({"workspace_id": 3, "user_id": 5, "role": "member"})

name_to_id = {u["name"]: u["id"] for u in users}
nodes = []
for l in logs_fe:
    nodes.append({
        "id": l["id"], "workspace_id": l["workspace_id"], "type": l["type"], "status": l["status"],
        "title": l["title"], "body": l["body"], "author_id": name_to_id[l["author_name"]],
        "error_code": l.get("error_code"), "keywords": l.get("keywords", []), "resolution_summary": l.get("resolution_summary"),
        "refine_status": l.get("refine_status", "pending"),
        "last_confirmed_at": l.get("last_confirmed_at"), "deprecated_via": l.get("deprecated_via"), "deprecate_reason": l.get("deprecate_reason"),
        "resolved_at": l.get("resolved_at"), "resolve_note": l.get("resolve_note"),
        "source": "synthetic", "source_ref": f"fe-mock-{l['id']}", "created_at": l["created_at"],
        # 평가셋 원천: 체인별 기대 질의는 데이터 담당이 채운다
        "expected_queries": [],
    })
node_ws = {n["id"]: n["workspace_id"] for n in nodes}
edges = [{"workspace_id": node_ws[e["parent_id"]], "parent_id": e["parent_id"], "child_id": e["child_id"],
          "created_by": nodes[[n["id"] for n in nodes].index(e["child_id"])]["author_id"], "origin": "manual"} for e in edges_fe]

documents = []
for d in docs_fe:
    documents.append({
        "id": d["id"], "workspace_id": d["workspace_id"], "slug": d["slug"], "title": d["title"], "version": d["version"],
        "doc_type": None, "source_type": d["source_type"], "file_path": (f"policies/{d['slug']}.pdf" if d.get("has_file") else None),
        "status": d["status"], "retired_at": d.get("retired_at"), "retire_reason": d.get("retire_reason"), "retired_by": d.get("retired_by"),
        "uploaded_by": d.get("uploaded_by") or "script", "uploaded_at": d.get("uploaded_at"), "indexed_at": d.get("indexed_at"),
    })
slug_to_doc = {d["slug"]: d["id"] for d in documents}
chunks = []
for slug, items in chunks_fe.items():
    for c in items:
        chunks.append({"id": c["id"], "doc_id": slug_to_doc[slug], "workspace_id": next(d["workspace_id"] for d in documents if d["id"] == slug_to_doc[slug]),
                       "article_label": c["article_label"], "page": c.get("page"), "text": c["text"], "bboxes": None})

audit = [{"tenant_id": 1, "workspace_id": e["workspace_id"], "table_name": "policy_documents", "row_id": str(e["document_id"]),
          "action": e["action"], "actor": e["actor"], "source": e["source"], "reason": e.get("reason"), "label": e["document_label"], "at": e["at"]}
         for e in events_fe]

dump("tenants.json", [tenant]); dump("workspaces.json", workspaces); dump("users.json", users); dump("workspace_members.json", members)
dump("log_nodes.json", nodes); dump("log_edges.json", edges)
dump("policy_documents.json", documents); dump("policy_chunks.json", chunks); dump("audit_log.json", audit)
print(f"workspaces {len(workspaces)} users {len(users)} members {len(members)} nodes {len(nodes)} edges {len(edges)} docs {len(documents)} chunks {len(chunks)} audit {len(audit)}")
