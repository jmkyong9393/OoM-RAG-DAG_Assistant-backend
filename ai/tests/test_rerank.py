"""규정 검색 재순위: 모델이 고른 순서 + 나머지 원래 순서, 실패하면 원래 순서."""
import asyncio

from app.retrieval import rerank


def _rows(n):
    return [{"id": f"c{i}", "doc_title": "규정", "article_label": f"제{i}조", "text": "본문"} for i in range(1, n + 1)]


def test_picked_order_puts_choices_first_and_keeps_the_rest():
    rows = _rows(6)
    out = rerank.picked_order(rows, [5, "2", 5, 99, 0, "x"])
    assert [r["id"] for r in out] == ["c5", "c2", "c1", "c3", "c4", "c6"], "목록 밖 번호·중복·숫자 아닌 값은 버린다"


def test_picked_order_without_choices_is_the_original():
    rows = _rows(3)
    assert rerank.picked_order(rows, None) == rows and rerank.picked_order(rows, []) == rows


def test_rerank_falls_back_to_search_order_on_error(monkeypatch):
    class Prof:
        has_key, model_light = True, "m"

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(rerank, "ai_mode", lambda: "openai")
    monkeypatch.setattr(rerank.llm, "_chat_json", boom)
    rows = _rows(4)
    assert asyncio.run(rerank.rerank(Prof(), "q", rows, rerank.llm.Usage())) == rows


def test_rerank_uses_model_choice(monkeypatch):
    class Prof:
        has_key, model_light = True, "m"

    seen = {}

    async def fake(profile, model, system, user, usage):
        seen["user"] = user
        return {"ids": [3, 1]}

    monkeypatch.setattr(rerank, "ai_mode", lambda: "openai")
    monkeypatch.setattr(rerank.llm, "_chat_json", fake)
    out = asyncio.run(rerank.rerank(Prof(), "환불 언제", _rows(4), rerank.llm.Usage()))
    assert [r["id"] for r in out] == ["c3", "c1", "c2", "c4"], "검색 1·2위는 이미 상위 5 안에 있다"
    assert "[질문]\n환불 언제" in seen["user"] and "[3] 규정 제3조: 본문" in seen["user"]


def test_keep_top_puts_search_top_two_back_into_the_top_five():
    rows = _rows(8)
    order = rerank.picked_order(rows, [8, 7, 6, 5, 4])           # 모델이 검색 1·2위를 안 골랐다
    out = [r["id"] for r in rerank.keep_top(order, rows)]
    assert out[:5] == ["c8", "c7", "c6", "c2", "c1"], "재순위가 뒤에 둔 것부터 바꾼다"
    assert sorted(out) == sorted(r["id"] for r in rows), "나머지는 빠지지 않는다"
