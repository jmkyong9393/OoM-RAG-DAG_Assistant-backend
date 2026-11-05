"""index_policy.order_by_supersedes — 개정 사슬 순서(대체되는 문서 먼저). DB 불필요."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import index_policy as ip  # noqa: E402


def test_superseded_document_is_indexed_first():
    docs = [{"slug": "retail-vpn-2026-11", "supersedes": "vpn-guide-v22"}, {"slug": "a-doc"},
            {"slug": "vpn-guide-v22", "supersedes": "vpn-guide-v21"}]          # 파일명 순: r… 이 v… 보다 앞
    order = [d["slug"] for d in ip.order_by_supersedes(docs)]
    assert order.index("vpn-guide-v22") < order.index("retail-vpn-2026-11")
    assert sorted(order) == sorted(d["slug"] for d in docs)                    # 빠지거나 겹치지 않는다


def test_cycle_does_not_loop():
    docs = [{"slug": "a", "supersedes": "b"}, {"slug": "b", "supersedes": "a"}]
    assert sorted(d["slug"] for d in ip.order_by_supersedes(docs)) == ["a", "b"]
