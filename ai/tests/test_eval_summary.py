"""검색 평가 요약(eval/hit_at_k.py summarize) — 대시보드에 기록하는 값. DB 불필요."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
import hit_at_k  # noqa: E402


def row(kind, hit, rank, ms=10.0):
    return {"kind": kind, "hybrid": {"hit": hit, "rank": rank}, "search_ms": ms}


def test_summarize_counts_hits_by_kind_and_mrr_within_k():
    s = hit_at_k.summarize([row("policy", True, 1), row("policy", True, 4), row("policy", False, None), row("troubleshoot", True, 2, 30.0)], "hybrid", 5)
    assert s["n"] == 4 and s["hit_at_k"] == 0.75
    assert s["meta"]["by_kind"] == {"policy": {"hit": 2, "n": 3}, "troubleshoot": {"hit": 1, "n": 1}}
    assert s["meta"]["mrr"] == round((1 + 0.25 + 0 + 0.5) / 4, 4), "상위 k 밖은 0 으로 센다"
    assert s["meta"]["search_ms_p50"] == 10.0
