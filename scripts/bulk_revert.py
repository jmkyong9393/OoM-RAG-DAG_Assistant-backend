"""일괄 등록(POST /logs/bulk)으로 넣은 사례를 적재 전 상태로 되돌린다 — 출처(source_ref) 접두어로 골라 지우고, 적재 전 스냅샷과 대조한다.

  snapshot  적재 직전에 한 번. 표마다 최대 id · 번호표 · 사용자 사전 · 지문(행 내용의 해시)을 JSON 으로 남긴다.
  revert    스냅샷 뒤에 들어온 '<접두어>…' 사례와 거기 딸린 행을 한 트랜잭션으로 지운다. --apply 가 없으면 지운 척만 하고 되돌린다(건수 확인).
  verify    스냅샷에 있던 행들이 그대로인지 표마다 대조하고, 스냅샷 뒤에 새로 생긴 행 수를 보여 준다.

사용(로컬):  set -a; . deploy/.env; set +a
            uv run --with "psycopg[binary]" scripts/bulk_revert.py snapshot --out /tmp/before_notebook.json --label notebook
            uv run --with "psycopg[binary]" scripts/bulk_revert.py revert --snapshot /tmp/before_notebook.json --prefix notebook: [--apply] ...
            uv run --with "psycopg[binary]" scripts/bulk_revert.py verify --snapshot /tmp/before_notebook.json
운영: bootstrap 컨테이너에서 /app/.venv/bin/python /src/scripts/bulk_revert.py … (DATABASE_URL 은 컨테이너 환경)
절차 · 판단 기준은 개발문서 48_사례_일괄_적재와_되돌리기.md.

되돌린 뒤에는 ai 를 다시 띄운다(사용자 사전을 프로세스가 들고 있다) — 운영: deploy/ 에서 FORCE="ai" bash rollout.sh ai
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

# 지문에 쓰는 칼럼 — 토큰 · 임베딩 · 갱신 시각처럼 워커가 다시 채우는 값은 뺀다
FINGERPRINTS = {
    "log_nodes": ("id", """id, workspace_id, type, status, title, md5(body), category, resolved_at, resolve_note, deprecated_via,
                    source, source_ref, md5(coalesce(attributes::text, '')), error_code, author_id"""),
    "log_edges": (None, "workspace_id, parent_id, child_id, origin, reason"),
    "log_resolutions": (None, "workspace_id, root_id, node_id"),
    "chain_summaries": (None, "root_id"),
    "incident_reports": ("id", "id, root_id, audience"),
    "workspace_terms": (None, "workspace_id, term, source"),
    "search_sessions": ("id", "id, workspace_id, user_id, query"),
    "answer_feedback": ("id", "id, session_id"),
    "eval_runs": ("id", "id, workspace_id, run_at"),
    "audit_log": ("id", "id, table_name, row_id, action, actor"),
}
ID_TABLES = [t for t, (k, _) in FINGERPRINTS.items() if k == "id"]


def fingerprint(cur, table: str, max_id: int | None = None) -> dict:
    key, cols = FINGERPRINTS[table]
    where = f"WHERE id <= {int(max_id)}" if (key == "id" and max_id is not None) else ""
    cur.execute(f"SELECT count(*), coalesce(md5(string_agg(r, '|' ORDER BY r)), '') FROM (SELECT ({cols})::text AS r FROM {table} {where}) x")
    n, h = cur.fetchone()
    return {"count": n, "md5": h}


def snapshot(conn, out: str, label: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT now(), current_database()")
        taken_at, dbname = cur.fetchone()
        max_id, seq = {}, {}
        for t in ID_TABLES:
            cur.execute(f"SELECT coalesce(max(id), 0) FROM {t}")
            max_id[t] = cur.fetchone()[0]
            cur.execute(f"SELECT pg_get_serial_sequence('{t}', 'id')")
            s = cur.fetchone()[0]
            if s:
                cur.execute(f"SELECT last_value, is_called FROM {s}")
                lv, called = cur.fetchone()
                seq[t] = {"name": s, "last_value": lv, "is_called": called}
        cur.execute("SELECT workspace_id, term, source FROM workspace_terms ORDER BY 1, 2")
        terms = [list(r) for r in cur.fetchall()]
        fps = {t: fingerprint(cur, t) for t in FINGERPRINTS}
    snap = {"label": label, "taken_at": taken_at.isoformat(), "database": dbname, "max_id": max_id, "sequences": seq,
            "terms": terms, "fingerprints": fps}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    print(f"스냅샷 {out} — {taken_at:%Y-%m-%d %H:%M:%S} · 사례 {fps['log_nodes']['count']} · 사전 {len(terms)} · 접속 기록 최대 id {max_id['audit_log']}")


def revert(conn, snap: dict, prefix: str, apply: bool, sessions: str | None, evals: str | None, keep_audit: bool) -> int:
    mx = snap["max_id"]
    cur = conn.cursor()
    cur.execute("SELECT current_database()")
    if cur.fetchone()[0] != snap["database"]:
        sys.exit(f"스냅샷의 DB({snap['database']})와 지금 DB 가 다르다")
    cur.execute("""SELECT id, workspace_id FROM log_nodes WHERE source = 'bulk' AND source_ref LIKE %s AND id > %s ORDER BY id""",
                (prefix + "%", mx["log_nodes"]))
    rows = cur.fetchall()
    target = [r[0] for r in rows]
    wss = sorted({r[1] for r in rows})
    cur.execute("SELECT count(*) FROM log_nodes WHERE source_ref LIKE %s AND id <= %s", (prefix + "%", mx["log_nodes"]))
    older = cur.fetchone()[0]
    print(f"대상 사례: {len(target)}건 (부서 {wss}, id {target[:1]}…{target[-1:]})" + (f" · 스냅샷 전부터 있던 같은 접두어 {older}건은 건드리지 않음" if older else ""))
    if not target:
        print("되돌릴 것이 없다")
        return 0

    blockers = []
    cur.execute("SELECT count(*) FROM log_edges WHERE (parent_id = ANY(%s)) <> (child_id = ANY(%s))", (target, target))
    if (n := cur.fetchone()[0]):
        blockers.append(f"대상 사례와 다른 사례를 잇는 연결 {n}개 — 적재 뒤 사람이 이었다. 먼저 화면에서 끊거나 내용을 확인할 것")
    cur.execute("SELECT count(*) FROM log_resolutions WHERE (root_id = ANY(%s)) <> (node_id = ANY(%s))", (target, target))
    if (n := cur.fetchone()[0]):
        blockers.append(f"대상과 다른 사례 사이의 해결 지정 {n}개")
    cur.execute("SELECT count(*) FROM log_nodes WHERE deprecated_via = ANY(%s) AND NOT id = ANY(%s)", (target, target))
    if (n := cur.fetchone()[0]):
        blockers.append(f"대상 사례로 대체 · 폐기된 다른 사례 {n}개")
    ntext = [f"n:{i}" for i in target]
    cur.execute("""SELECT id FROM search_sessions
                   WHERE retrieved_ids && %s::text[] OR verified_ids && %s::text[] OR unverified_ids && %s::text[]
                      OR (chain_ref->>'entry_id')::bigint = ANY(%s)
                      OR EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(chain_ref->'nodes') = 'array' THEN chain_ref->'nodes' ELSE '[]' END) e
                                 WHERE (e->>'id')::bigint = ANY(%s))""", (ntext, ntext, ntext, target, target))
    sess = [r[0] for r in cur.fetchall()]
    if sess and sessions is None:
        blockers.append(f"대상 사례를 근거로 쓴 질문 기록 {len(sess)}건 — --sessions drop(지움) 또는 --sessions keep(남김, 사례를 누르면 404) 중 하나를 고를 것")
    cur.execute("SELECT id FROM eval_runs WHERE id > %s", (mx["eval_runs"],))
    ev = [r[0] for r in cur.fetchall()]
    if ev and evals is None:
        blockers.append(f"스냅샷 뒤의 측정 결과 {len(ev)}건 — 적재된 상태를 잰 값이면 --evals drop, 남길 거면 --evals keep")
    cur.execute("SELECT count(*) FROM audit_log WHERE id > %s AND action = 'bulk_import' AND workspace_id = ANY(%s)", (mx["audit_log"], wss))
    imports = cur.fetchone()[0]
    if imports > 1 and not keep_audit:
        blockers.append(f"스냅샷 뒤 같은 부서의 일괄 등록 기록이 {imports}건 — 다른 일괄 등록과 섞였을 수 있다. 확인 뒤 --keep-audit 로 진행")
    if blockers:
        print("멈춤 — 아래를 먼저 정리한다:")
        for b in blockers:
            print("  -", b)
        conn.rollback()
        return 2

    done = {}
    def run(name: str, sql: str, params=()):
        cur.execute(sql, params)
        done[name] = cur.rowcount

    if sess and sessions == "drop":
        run("답변 평가", "DELETE FROM answer_feedback WHERE session_id = ANY(%s)", (sess,))
        run("질문 기록", "DELETE FROM search_sessions WHERE id = ANY(%s)", (sess,))
    run("장애 보고서", "DELETE FROM incident_reports WHERE root_id = ANY(%s)", (target,))
    run("체인 요약", "DELETE FROM chain_summaries WHERE root_id = ANY(%s)", (target,))
    run("해결 지정", "DELETE FROM log_resolutions WHERE root_id = ANY(%s) OR node_id = ANY(%s)", (target, target))
    run("연결", "DELETE FROM log_edges WHERE parent_id = ANY(%s) OR child_id = ANY(%s)", (target, target))
    cur.execute("UPDATE log_nodes SET deprecated_via = NULL WHERE id = ANY(%s) AND deprecated_via IS NOT NULL", (target,))
    run("사례", "DELETE FROM log_nodes WHERE id = ANY(%s)", (target,))
    # 사용자 사전: 스냅샷 뒤에 생긴 자동 용어 중 남은 사례가 더는 쓰지 않는 것만
    before = {(w, t) for w, t, _ in snap["terms"]}
    cur.execute("""SELECT workspace_id, term FROM workspace_terms wt
                   WHERE source = 'auto' AND created_at >= %s::timestamptz
                     AND NOT EXISTS (SELECT 1 FROM log_nodes n WHERE n.workspace_id = wt.workspace_id
                                     AND (wt.term = ANY(n.keywords) OR n.error_code = wt.term))""", (snap["taken_at"],))
    drop_terms = [(w, t) for w, t in cur.fetchall() if (w, t) not in before]
    for w, t in drop_terms:
        cur.execute("DELETE FROM workspace_terms WHERE workspace_id = %s AND term = %s AND source = 'auto'", (w, t))
    done["자동 용어"] = len(drop_terms)
    if ev and evals == "drop":
        run("측정 결과", "DELETE FROM eval_runs WHERE id = ANY(%s)", (ev,))
    if keep_audit:
        cur.execute("""INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
                       SELECT w.tenant_id, w.id, 'log_nodes', '-', 'bulk_reverted', 'script', 'script', %s FROM workspaces w WHERE w.id = ANY(%s)""",
                    (f"prefix={prefix} nodes={len(target)}", wss))
        done["접속 기록(되돌림 기록 추가)"] = cur.rowcount
    else:
        run("접속 기록", """DELETE FROM audit_log WHERE id > %s AND ((table_name = 'log_nodes' AND row_id = ANY(%s))
                                                       OR (action = 'bulk_import' AND workspace_id = ANY(%s)))""",
            (mx["audit_log"], [str(i) for i in target], wss))
    # 번호표: 스냅샷 뒤에 다른 행이 생기지 않았으면 스냅샷 값으로 되돌린다
    for t, s in snap["sequences"].items():
        cur.execute(f"SELECT coalesce(max(id), 0) FROM {t}")
        if cur.fetchone()[0] <= mx[t]:
            cur.execute("SELECT setval(%s, %s, %s)", (s["name"], s["last_value"], s["is_called"]))
        else:
            print(f"  번호표 {t}: 스냅샷 뒤에 다른 행이 있어 그대로 둠")
    for k, v in done.items():
        print(f"  {k}: {v}")
    if apply:
        conn.commit()
        print("적용함. ai 를 다시 띄울 것(사용자 사전) — 운영: FORCE=\"ai\" bash rollout.sh ai")
    else:
        conn.rollback()
        print("미리 보기 — 되돌렸다. 적용하려면 --apply")
    return 0


def verify(conn, snap: dict) -> int:
    mx = snap["max_id"]
    bad = 0
    with conn.cursor() as cur:
        for t, before in snap["fingerprints"].items():
            key, _ = FINGERPRINTS[t]
            now_old = fingerprint(cur, t, mx.get(t)) if key == "id" else fingerprint(cur, t)
            same = now_old == before
            extra = ""
            if key == "id":
                cur.execute(f"SELECT count(*) FROM {t} WHERE id > %s", (mx[t],))
                extra = f" · 스냅샷 뒤 새 행 {cur.fetchone()[0]}"
            print(f"  {'같음' if same else '다름'}  {t}: 스냅샷 {before['count']} → 지금 {now_old['count']}{extra}")
            bad += 0 if same else 1
        cur.execute("SELECT workspace_id, term, source FROM workspace_terms")
        now_terms = {tuple(r) for r in cur.fetchall()}
        before_terms = {tuple(r) for r in snap["terms"]}
        print(f"  사전: 사라진 {len(before_terms - now_terms)} · 새로 생긴 {len(now_terms - before_terms)}")
    print("모든 표가 스냅샷과 같다" if not bad else f"다른 표 {bad}개 — 스냅샷 뒤의 다른 작업인지 확인할 것")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--out", required=True); s.add_argument("--label", default="")
    r = sub.add_parser("revert")
    r.add_argument("--snapshot", required=True); r.add_argument("--prefix", required=True)
    r.add_argument("--apply", action="store_true")
    r.add_argument("--sessions", choices=["drop", "keep"]); r.add_argument("--evals", choices=["drop", "keep"])
    r.add_argument("--keep-audit", action="store_true", help="접속 기록을 지우지 않고 '되돌림' 기록을 한 줄 더한다")
    v = sub.add_parser("verify"); v.add_argument("--snapshot", required=True)
    a = ap.parse_args()
    if a.cmd == "revert" and not a.prefix.strip(":"):
        sys.exit("--prefix 가 비었다")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if a.cmd == "snapshot":
            snapshot(conn, a.out, a.label)
            return 0
        with open(a.snapshot, encoding="utf-8") as f:
            snap = json.load(f)
        if a.cmd == "revert":
            return revert(conn, snap, a.prefix, a.apply, a.sessions, a.evals, a.keep_audit)
        return verify(conn, snap)


if __name__ == "__main__":
    sys.exit(main())
