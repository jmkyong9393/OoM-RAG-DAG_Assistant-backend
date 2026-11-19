"""합성 로그의 ref → 노드 id 를 DB 에서 찾는다 (load_chain_records · load_alignment 가 쓴다).

노드 id 는 DB 마다 다르다 — data/logs/<파일>.ids.json 은 그것을 만든 DB 의 번호라, 다른 PC 의 DB 에서는 엉뚱한 노드를 가리킨다.
그래서 파일의 번호를 쓰지 않고, 로그 파일의 ref → source_ref 와 DB 의 (부서, source_ref) → id 로 찾는다.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def node_ids(conn, log: str, cache: dict) -> dict:
    """{"workspace_id": 부서, "ref": {ref: 노드 id}} — 이 DB 에 적재된 노드만 담는다. 적재되지 않은 ref 는 빠진다(호출 측이 경고한다)."""
    if log not in cache:
        f = ROOT / "data" / "logs" / f"{log}.json"
        if not f.exists():
            raise SystemExit(f"{f.name} 이 없다 — 대조표의 log 이름을 확인한다")
        doc = json.loads(f.read_text(encoding="utf-8"))
        ws = doc["workspace_id"]
        by_source = dict(conn.execute("SELECT source_ref, id FROM log_nodes WHERE workspace_id = %s AND source_ref IS NOT NULL", (ws,)).fetchall())
        if not by_source:
            raise SystemExit(f"{f.name}: 부서 {ws} 에 적재된 로그가 없다. make load-logs 를 먼저 실행한다")
        cache[log] = {"workspace_id": ws, "ref": {it["ref"]: by_source[it["source_ref"]] for it in doc["items"] if it.get("source_ref") in by_source}}
    return cache[log]
