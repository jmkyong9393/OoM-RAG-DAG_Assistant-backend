"""공통 픽스처. DB 통합 테스트는 로컬 compose Postgres(시드 적용)가 떠 있을 때만 돈다.
  관리자 DSN: TEST_ADMIN_DB_URL → DATABASE_URL → 기본 postgresql://oom:oom@localhost:5432/oom — 픽스처 삽입·정리용
  앱 DSN   : AI_DB_URL (기본 oom_ai 계정, :5432) — 앱 코드가 쓰는 권한으로만 동작하는지 검증
  env 가 비어 있으면 deploy/.env 를 읽어 채운다 (호스트마다 포트가 달라도 `uv run pytest` 만으로 돌게).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parents[2] / "deploy" / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split("#", 1)[0].strip().strip('"')
        os.environ.setdefault(k.strip(), v)


_load_dotenv()
ADMIN_DSN = os.environ.get("TEST_ADMIN_DB_URL") or os.environ.get("DATABASE_URL") or "postgresql://oom:oom@localhost:5432/oom"


def parse_blocks(text: str | bytes) -> list[tuple[str, object]]:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    blocks = []
    for raw in text.split("\n\n"):
        if not raw.strip():
            continue
        ev, data = None, []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        blocks.append((ev, json.loads("\n".join(data))))
    return blocks


async def collect(agen) -> list[tuple[str, object]]:
    buf = b""
    async for chunk in agen:
        buf += chunk
    return parse_blocks(buf)


def _db_reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(ADMIN_DSN, connect_timeout=2) as c:
            return c.execute("SELECT count(*) FROM log_nodes").fetchone()[0] > 0
    except Exception:  # noqa: BLE001
        return False


DB_AVAILABLE = _db_reachable()
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason=f"local seeded Postgres not reachable ({ADMIN_DSN.rsplit('@', 1)[-1]})")


@pytest.fixture
def admin_conn():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(ADMIN_DSN, row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.fixture
def fake_mode(monkeypatch):
    monkeypatch.setenv("AI_MODE", "fake")
    monkeypatch.delenv("LLM_PROFILE_DEFAULT_API_KEY", raising=False)


@pytest.fixture
def dummy_mode(monkeypatch):
    monkeypatch.setenv("AI_MODE", "dummy")


@pytest.fixture(scope="session")
def backfilled():
    """워커 1회 실행: 시드 청크·노드의 tokens 백필 (어휘 검색 전제). DB 없으면 건너뜀."""
    if not DB_AVAILABLE:
        pytest.skip("db")
    import asyncio
    os.environ.setdefault("AI_MODE", "fake")
    from app import worker
    return asyncio.run(worker.run_once())
