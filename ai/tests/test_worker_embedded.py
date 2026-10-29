"""내장 워커: WORKER_EMBEDDED=1 이면 lifespan 이 worker.loop() 를 데몬 스레드로 띄운다(기본은 끔). DB·LLM 불필요."""
import asyncio
import threading

import pytest

from app import main as app_main
from app import worker


def _no_db(*a, **k):
    raise ConnectionError("no db in this test")


@pytest.mark.asyncio
async def test_lifespan_starts_embedded_worker_thread(monkeypatch):
    monkeypatch.setenv("AI_MODE", "fake")
    monkeypatch.setenv("WORKER_EMBEDDED", "1")
    started = threading.Event()

    async def fake_loop():
        started.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "loop", fake_loop)
    monkeypatch.setattr("app.retrieval.tokenize.warm_up", lambda terms: "")
    monkeypatch.setattr("app.retrieval.db.connect", _no_db)
    async with app_main.lifespan(app_main.app):
        assert started.wait(timeout=5), "워커 스레드가 시작되지 않았다"
        t = app_main.app.state.worker_thread
        assert t.daemon and t.name == "refine-worker"


@pytest.mark.asyncio
async def test_lifespan_without_flag_does_not_start_worker(monkeypatch):
    monkeypatch.setenv("AI_MODE", "fake")
    monkeypatch.delenv("WORKER_EMBEDDED", raising=False)
    called = threading.Event()
    monkeypatch.setattr(worker, "start_embedded", lambda: called.set())
    monkeypatch.setattr("app.retrieval.tokenize.warm_up", lambda terms: "")
    monkeypatch.setattr("app.retrieval.db.connect", _no_db)
    async with app_main.lifespan(app_main.app):
        assert not called.is_set()
