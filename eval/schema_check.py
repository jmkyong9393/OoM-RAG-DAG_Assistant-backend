"""평가를 돌리기 전에 대상 DB 의 스키마가 레포의 마이그레이션과 같은 판인지 본다.
복제 DB(oom_eval)가 옛 판에 멈춰 있으면 새 DB 함수가 없어 사례를 못 불러오고, 점검이 이유 없이 떨어진다(개정 점검 10/10 → 4/10)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "api" / "src" / "main" / "resources" / "db" / "migration"


def repo_version(folder: Path = MIGRATIONS) -> int:
    """레포의 가장 높은 V 번호."""
    return max((int(m.group(1)) for f in folder.glob("V*__*.sql") if (m := re.match(r"V(\d+)__", f.name))), default=0)


def db_version(conn) -> int:
    row = conn.execute("SELECT max(version::int) AS v FROM flyway_schema_history WHERE success AND version ~ '^[0-9]+$'").fetchone()
    return (row["v"] if isinstance(row, dict) else row[0]) or 0


def stale_message(db: int, repo: int, name: str) -> str | None:
    if db >= repo:
        return None
    return (f"DB '{name}' 의 스키마가 V{db} 이고 레포는 V{repo} 입니다 — 이대로 재면 결과가 틀립니다.\n"
            f"  맞추기: POSTGRES_DB={name} docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile migrate run --rm flyway\n"
            f"  (그래도 돌리려면 --allow-stale-schema)")


def ensure_current(allow_stale: bool = False) -> None:
    """스키마가 뒤처져 있으면 안내하고 끝낸다. 평가 도구의 main 첫머리에서 부른다."""
    from app.retrieval import db
    with db.connect() as conn:
        name = conn.execute("SELECT current_database() AS d").fetchone()["d"]
        msg = stale_message(db_version(conn), repo_version(), name)
    if msg:
        print(msg, file=sys.stderr)
        if not allow_stale:
            sys.exit(2)
