"""data/eval/eval_runs.json → eval_runs. 저장해 둔 평가 결과를 대시보드 지표(검색 품질 · 충실도 · 관련성)로 넣는다 — LLM·임베딩 호출 없음.

파일은 eval/export_eval_runs.py 가 hit_at_k.py · answer_quality.py 의 --json 결과로 만든다(--record 가 넣는 행과 같은 모양 + 측정 시각).
같은 부서 · 데이터셋 · 모드 · 측정 시각의 행이 이미 있으면 건너뛴다(멱등). 운영 DB 는 bootstrap 의 eval 단계가 부른다.

사용:  DATABASE_URL=... python scripts/load_eval_runs.py [파일]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "data" / "eval" / "eval_runs.json"
METRICS = ("hit_at_5", "faithfulness", "answer_relevance")


def insert(conn, rows: list[dict]) -> int:
    """eval_runs 에 행을 넣고 넣은 수를 돌려준다. run_at 이 있는 행은 같은 행이 이미 있으면 건너뛴다(없으면 now())."""
    from psycopg.types.json import Jsonb
    n = 0
    for r in rows:
        run_at = r.get("run_at")
        if run_at and conn.execute(
                """SELECT 1 FROM eval_runs WHERE workspace_id IS NOT DISTINCT FROM %s AND dataset_version = %s
                   AND mode IS NOT DISTINCT FROM %s AND run_at = %s::timestamptz""",
                (r["workspace_id"], r["dataset_version"], r.get("mode"), run_at)).fetchone():
            continue
        conn.execute(
            """INSERT INTO eval_runs (workspace_id, dataset_version, mode, is_sample, hit_at_5, faithfulness, answer_relevance, n_samples, meta, run_at)
               VALUES (%s, %s, %s, false, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))""",
            (r["workspace_id"], r["dataset_version"], r.get("mode"), *(r.get(m) for m in METRICS), r["n_samples"], Jsonb(r.get("meta") or {}), run_at))
        n += 1
    return n


def main() -> int:
    import psycopg
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print(f"평가 결과 파일이 없습니다: {path}"); return 1
    rows = json.loads(path.read_text(encoding="utf-8"))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        n = insert(conn, rows)
    print(f"eval_runs: {n}행 넣음 · {len(rows) - n}행 이미 있음 ({path.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
