"""저장된 평가 결과 → eval_runs (eval/export_eval_runs.py · scripts/load_eval_runs.py). 적재 멱등성만 DB 필요."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))
import export_eval_runs as ex  # noqa: E402
import hit_at_k  # noqa: E402
import load_eval_runs  # noqa: E402

from tests.conftest import ADMIN_DSN, requires_db  # noqa: E402


def hit_row(ws, kind, hit, rank):
    r = {"workspace_id": ws, "kind": kind, "search_ms": 10.0}
    r["hybrid"] = {"hit": hit, "rank": rank}
    r["rerank"] = {"hit": True, "rank": 1}
    return r


def quality_row(ws, f, rel):
    return {"workspace_id": ws, "faithfulness": f, "answer_relevance": rel, "statements": 4, "supported": 4 if f == 1.0 else 3,
            "noncommittal": False, "no_answer": f is None}


def test_hit_eval_rows_has_overall_and_per_workspace_rows():
    rows = hit_at_k.eval_rows([hit_row(1, "policy", True, 1), hit_row(1, "policy", False, None), hit_row(2, "troubleshoot", True, 2)],
                              "hybrid", 5, "emb", "evalset v1")
    by_ws = {r["workspace_id"]: r for r in rows}
    assert set(by_ws) == {None, 1, 2}
    assert by_ws[None]["hit_at_5"] == round(2 / 3, 4) and by_ws[None]["n_samples"] == 3
    assert by_ws[1]["hit_at_5"] == 0.5 and by_ws[1]["mode"] == "hybrid"
    assert by_ws[None]["meta"]["embedding_model"] == "emb" and "faithfulness" not in by_ws[None]


def test_export_picks_rerank_column_and_stamps_measured_time(tmp_path):
    hit = tmp_path / "hit.json"
    hit.write_text(json.dumps({"k": 5, "summary": {"hybrid": {}, "rerank": {}},
                               "rows": [hit_row(1, "policy", False, None), hit_row(1, "policy", True, 3)]}), encoding="utf-8")
    os.utime(hit, (1_700_000_000, 1_700_000_000))
    rows = ex.from_hit(hit, None, "emb", "실험 A")
    total = next(r for r in rows if r["workspace_id"] is None)
    assert total["mode"] == "rerank" and total["hit_at_5"] == 1.0, "재순위 열이 있으면 그 열을 기록한다"
    assert total["run_at"] == "2023-11-14T22:13:20+00:00"
    assert total["meta"]["source"] == f"실험 A · {tmp_path.parent.name}/{tmp_path.name}/hit.json"


def test_export_rejects_non_k5(tmp_path):
    hit = tmp_path / "hit.json"
    hit.write_text(json.dumps({"k": 3, "summary": {"hybrid": {}}, "rows": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ex.from_hit(hit, None, "emb", "")


def test_export_quality_needs_answer_model(tmp_path):
    q = tmp_path / "q.json"
    body = {"judge_model": "judge", "relevance_embedding": "emb", "relevance_baseline": 0.27,
            "rows": [quality_row(1, 1.0, 0.7), quality_row(2, 0.75, 0.6)]}
    q.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(SystemExit):
        ex.from_quality(q, None, "")
    total = next(r for r in ex.from_quality(q, "answer", "") if r["workspace_id"] is None)
    assert total["faithfulness"] == 0.875 and total["answer_relevance"] == 0.65
    assert total["meta"]["answer_model"] == "answer" and total["meta"]["relevance_baseline"] == 0.27
    q.write_text(json.dumps({**body, "answer_model": "saved"}), encoding="utf-8")
    assert next(r for r in ex.from_quality(q, "answer", "") if r["workspace_id"] is None)["meta"]["answer_model"] == "saved", "파일의 기록이 우선"


@requires_db
def test_load_is_idempotent_by_measured_time():
    import psycopg
    rows = [{"workspace_id": None, "dataset_version": "test v0", "mode": "rerank", "hit_at_5": 0.5, "n_samples": 2,
             "meta": {"k": 5}, "run_at": "2020-01-01T00:00:00+00:00"},
            {"workspace_id": 1, "dataset_version": "test v0", "mode": "hybrid", "faithfulness": 0.9, "answer_relevance": 0.6,
             "n_samples": 1, "meta": {}, "run_at": "2020-01-01T00:00:00+00:00"}]
    with psycopg.connect(ADMIN_DSN) as conn:
        try:
            assert load_eval_runs.insert(conn, rows) == 2
            assert load_eval_runs.insert(conn, rows) == 0, "같은 측정 시각이면 다시 넣지 않는다"
            got = conn.execute("SELECT workspace_id, hit_at_5, faithfulness, is_sample, run_at FROM eval_runs WHERE dataset_version = 'test v0' ORDER BY id").fetchall()
            assert [(g[0], g[1], g[2], g[3]) for g in got] == [(None, 0.5, None, False), (1, None, 0.9, False)]
            assert got[0][4].year == 2020
        finally:
            conn.rollback()
