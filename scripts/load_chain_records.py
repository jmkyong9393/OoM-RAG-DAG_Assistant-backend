"""사례 체인 기록(data/chain_records/*.json)을 적재한다: 장애 건을 해결한 기록(log_resolutions)과 간선의 연결 사유(log_edges.reason).

  "resolutions":  [{"log": "it-dev_v1", "root": "d6-root", "resolved_by": ["d6-jitter", "d6-fallback"], "why": "…"}]
  "edge_reasons": [{"log": "it-dev_v1", "parent": "d6-ttl", "child": "d6-jitter", "reason": "…"}]
노드 id 는 data/logs/<파일>.ids.json 에서 찾는다. 재실행하면 같은 값으로 덮어쓴다(멱등) — 적힌 장애 건의 해결 기록은 통째로 바뀐다.
해결 기록은 그 장애 건의 활성 success 자손만, 연결 사유는 이미 있는 간선에만 쓴다. 맞지 않으면 경고하고 그 줄은 건너뛴다.

사용: python scripts/load_chain_records.py [파일...] [--dry-run]    (먼저 make load-logs 로 *.ids.json 을 만든다)
환경: DATABASE_URL (기본 postgresql://oom:oom@localhost:5432/oom) — make link-chains 는 deploy/.env 를 읽는다
"""
import argparse, json, os, pathlib, sys

import psycopg

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("DATABASE_URL", "postgresql://oom:oom@localhost:5432/oom")


def node_ids(log: str, cache: dict) -> dict:
    if log not in cache:
        f = ROOT / "data" / "logs" / f"{log}.ids.json"
        if not f.exists():
            sys.exit(f"{f.name} 이 없다. make load-logs 를 먼저 실행한다")
        cache[log] = json.loads(f.read_text(encoding="utf-8"))
    return cache[log]


def load(conn, path: pathlib.Path, dry: bool) -> tuple[int, int, list[str]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    cache, warn, roots, edges = {}, [], 0, 0
    for r in doc.get("resolutions", []):
        ids = node_ids(r["log"], cache)
        ws, root = ids["workspace_id"], ids["ref"].get(r["root"])
        nodes = [ids["ref"].get(x) for x in r["resolved_by"]]
        if root is None or None in nodes:
            warn.append(f"ref 없음: {r['log']}/{r['root']} → {r['resolved_by']}"); continue
        row = conn.execute("SELECT type, resolved_at IS NOT NULL, NOT EXISTS (SELECT 1 FROM log_edges WHERE child_id = %s) FROM log_nodes WHERE id = %s AND workspace_id = %s",
                           (root, root, ws)).fetchone()
        if not row or row[0] != "fail" or not row[2]:
            warn.append(f"장애 건(fail 루트)이 아니다: {r['log']}/{r['root']}"); continue
        under = {x for (x,) in conn.execute("""SELECT g.node_id FROM get_chain(%s, %s, 32, 100000) g JOIN log_nodes n ON n.id = g.node_id
                                               WHERE g.depth > 0 AND n.type = 'success' AND n.status = 'active'""", (ws, root))}
        bad = [x for x, i in zip(r["resolved_by"], nodes) if i not in under]
        if bad:
            warn.append(f"{r['log']}/{r['root']}: 활성 success 자손이 아니다 {bad}"); continue
        if not row[1]:
            warn.append(f"{r['log']}/{r['root']}: 완료되지 않은 장애 건 — 해결 기록만 남긴다")
        if not dry:
            conn.execute("DELETE FROM log_resolutions WHERE root_id = %s", (root,))
            for i in nodes:
                conn.execute("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (%s, %s, %s)", (ws, root, i))
        roots += 1
    for e in doc.get("edge_reasons", []):
        ids = node_ids(e["log"], cache)
        p, c = ids["ref"].get(e["parent"]), ids["ref"].get(e["child"])
        reason = (e.get("reason") or "").strip()
        if p is None or c is None or not reason or len(reason) > 300:
            warn.append(f"연결 사유를 쓸 수 없다: {e['log']}/{e['parent']} → {e['child']}"); continue
        if dry:
            hit = conn.execute("SELECT count(*) FROM log_edges WHERE parent_id = %s AND child_id = %s", (p, c)).fetchone()[0]
        else:
            hit = conn.execute("UPDATE log_edges SET reason = %s WHERE parent_id = %s AND child_id = %s", (reason, p, c)).rowcount
        if not hit:
            warn.append(f"간선 없음: {e['log']}/{e['parent']} → {e['child']}"); continue
        edges += 1
    if not dry and (roots or edges):
        conn.execute("""INSERT INTO audit_log (tenant_id, table_name, row_id, action, actor, source, reason)
                        VALUES ((SELECT id FROM tenants WHERE slug = %s), 'log_resolutions', '-', 'chain_records_linked', 'script', 'script', %s)""",
                     (doc.get("tenant"), f"{path.name}: roots={roots} edges={edges}"))
    return roots, edges, warn


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=pathlib.Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    files = a.files or sorted((ROOT / "data" / "chain_records").glob("*.json"))
    bad = 0
    with psycopg.connect(DSN) as conn:
        for f in files:
            with conn.transaction():
                roots, edges, warn = load(conn, f, a.dry_run)
            print(f"{f.name}: 해결 기록 장애 건 {roots} · 연결 사유 {edges}" + (" (dry-run)" if a.dry_run else ""))
            for w in warn:
                print("  경고:", w)
            bad += len(warn)
    return 1 if bad and a.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
