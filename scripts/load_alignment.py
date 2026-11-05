"""규정-사례 대조표(data/alignment/*.json)를 사례 노드의 근거 조문으로 적재한다.

대조표의 한 줄(로그 파일·ref → 규정 slug·조문·관계)을 log_nodes.attributes.policy_refs 에 쓴다. 노드 id 는 data/logs/<파일>.ids.json 에서 찾는다.
  [{"policy": "retail-deploy-incident-2026-11", "article": "제12조", "relation": "follows", "note": "…"}]   # follows | exception
  [{"relation": "gap", "topic": "…", "note": "…"}]                                                          # 규정에 기준이 없음
재실행하면 같은 값으로 덮어쓴다(멱등). 대조표에 없는 노드의 policy_refs 는 건드리지 않는다.
가리키는 조문이 그 부서에서 검색되는 활성 문서에 없으면 경고만 낸다(답변에는 실리지 않는다) — 규정을 먼저 적재한다(make index).

사용: python scripts/load_alignment.py [대조표...] [--dry-run]    (먼저 make load-logs 로 *.ids.json 을 만든다)
환경: DATABASE_URL (기본 postgresql://oom:oom@localhost:5432/oom) — make link-policies 는 deploy/.env 를 읽는다
"""
import argparse, json, os, pathlib, sys

import psycopg
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("DATABASE_URL", "postgresql://oom:oom@localhost:5432/oom")
RELATIONS = ("follows", "exception", "gap")


def to_ref(link: dict) -> dict:
    if link["relation"] == "gap":
        return {k: link[k] for k in ("relation", "topic", "note") if link.get(k)}
    return {k: link[k] for k in ("policy", "article", "relation", "note") if link.get(k)}


def same_article(label: str, article: str) -> bool:
    head = (label or "").strip().lstrip("[")
    return head.startswith(article) and not head[len(article):len(article) + 1].isdigit()


def load(conn, path: pathlib.Path, dry: bool) -> tuple[int, int, list[str]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    ids, by_node, warn = {}, {}, []
    for link in doc["links"]:
        if link.get("relation") not in RELATIONS:
            sys.exit(f"{path.name}: relation 은 {RELATIONS} 중 하나 — {link}")
        log = link["log"]
        if log not in ids:
            f = ROOT / "data" / "logs" / f"{log}.ids.json"
            if not f.exists():
                sys.exit(f"{path.name}: {f.name} 이 없다. make load-logs 를 먼저 실행한다")
            ids[log] = json.loads(f.read_text(encoding="utf-8"))
        node = ids[log]["ref"].get(link["ref"])
        if node is None:
            warn.append(f"ref 없음: {log}/{link['ref']}")
            continue
        by_node.setdefault((node, ids[log]["workspace_id"]), []).append(link)
    updated = 0
    for (node, ws), links in by_node.items():
        row = conn.execute("SELECT workspace_id FROM log_nodes WHERE id = %s", (node,)).fetchone()
        if not row or row[0] != ws:
            warn.append(f"노드 {node} 가 ws{ws} 에 없다 — ids.json 이 지금 DB 와 다르다(make load-logs 다시)")
            continue
        for link in links:
            if link["relation"] == "gap":
                continue
            labels = [r[0] for r in conn.execute("""
                SELECT c.article_label FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
                WHERE d.status = 'active' AND d.slug = %s AND c.workspace_id IN (SELECT workspace_ancestors(%s))""", (link["policy"], ws))]
            if not any(same_article(l, link["article"]) for l in labels):
                warn.append(f"조문 없음: {link['policy']} {link['article']} (노드 {node}, ws{ws})")
        if not dry:
            conn.execute("UPDATE log_nodes SET attributes = coalesce(attributes, '{}'::jsonb) || jsonb_build_object('policy_refs', %s::jsonb) WHERE id = %s",
                         (Jsonb([to_ref(l) for l in links]), node))
        updated += 1
    if not dry and updated:
        conn.execute("""INSERT INTO audit_log (tenant_id, table_name, row_id, action, actor, source, reason)
                        VALUES ((SELECT id FROM tenants WHERE slug = %s), 'log_nodes', '-', 'policy_refs_linked', 'script', 'script', %s)""",
                     (doc.get("tenant"), f"{path.name}: nodes={updated} links={len(doc['links'])}"))
    return updated, len(doc["links"]), warn


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=pathlib.Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    files = a.files or sorted((ROOT / "data" / "alignment").glob("*.json"))
    bad = 0
    with psycopg.connect(DSN) as conn:
        for f in files:
            with conn.transaction():
                updated, total, warn = load(conn, f, a.dry_run)
            print(f"{f.name}: 노드 {updated} · 연결 {total}" + (" (dry-run)" if a.dry_run else ""))
            for w in warn:
                print("  경고:", w)
            bad += len(warn)
    return 1 if bad and a.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
