"""근거 마커 없이 '따라서'로 다시 맺는 문장만 걸러 낸다 — 조각이 어디서 끊겨 와도 결과가 같다."""
import pytest

from app.graph import pipeline
from app.graph.conclusion_filter import ConclusionFilter
from tests.conftest import collect, requires_db

RULE = '고객이 욕설을 하면 1차 경고하고, 계속되면 2차 경고 후 응대를 종료할 수 있습니다 [P1]. '
CASE = "사례에서는 2차 경고 후 통화를 종료했습니다 [N1]."


def run(text: str, size: int) -> tuple[str, list[str]]:
    f = ConclusionFilter()
    out = "".join(f.feed(text[i:i + size]) for i in range(0, len(text), size)) + f.close()
    return out, f.dropped


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_drops_an_unmarked_conclusion_and_keeps_the_paragraph_break(size):
    out, dropped = run(RULE + "따라서 폭언하는 고객의 전화를 먼저 끊는 것은 허용되지 않습니다. \n\n" + CASE, size)
    assert out == RULE + "\n\n" + CASE
    assert dropped == ["따라서 폭언하는 고객의 전화를 먼저 끊는 것은 허용되지 않습니다."]


@pytest.mark.parametrize("size", [1, 3, 1000])
def test_keeps_a_conclusion_that_carries_a_marker(size):
    for text in (RULE + "따라서 2차 경고 뒤에는 끊을 수 있습니다 [P1]. " + CASE,
                 RULE + "따라서 2차 경고 뒤에는 끊을 수 있습니다. [P1] " + CASE,     # 마침표 뒤에 붙은 마커
                 RULE + "따라서 2차 경고 뒤에는 끊을 수 있습니다 [P1]"):
        assert run(text, size) == (text, [])


@pytest.mark.parametrize("size", [1, 3, 1000])
def test_drops_an_unmarked_conclusion_at_the_end(size):
    assert run(RULE + "따라서, 끊는 것이 가능합니다.", size) == (RULE, ["따라서, 끊는 것이 가능합니다."])
    assert run("규정은 이렇습니다 [P1].\n따라서 가능합니다", size) == ("규정은 이렇습니다 [P1].\n", ["따라서 가능합니다"])


@pytest.mark.parametrize("size", [1, 3, 1000])
def test_leaves_everything_else_alone(size):
    for text in ("따라서 가능합니다.",                                   # 첫 문장은 걸러 내지 않는다(답이 비지 않게)
                 "기준에 따라서 처리합니다 [P1]. 절차에 따라 진행합니다 [P1].",  # 문장 머리가 아닌 '따라서'
                 "신청합니다 [P1]. 따라 하기 쉬운 절차입니다 [P2].",           # '따라'로 시작하는 다른 말
                 RULE + CASE,
                 "| 구분 | 기준 |\n| 욕설 | 2차 경고 후 종료 [P1] |"):
        assert run(text, size) == (text, [])


@requires_db
async def test_pipeline_streams_the_filtered_answer(fake_mode, backfilled, monkeypatch):
    """파이프라인이 내보내는 토큰에서 마커 없는 '따라서' 문장이 빠지고, 인용 검증은 남은 글로 한다."""
    async def generate(prof, query, policy_ctx, chain_ctx, usage, case_required=True):
        ref = policy_ctx[0]["ref"]
        for piece in (f"2차 경고 후 응대를 종료할 수 있습니다 [{ref}]. ", "따라", "서 먼저 끊는 것은 허용되지 않습니다. ", f"\n\n보호담당자에게 알립니다 [{ref}]."):
            yield piece
    monkeypatch.setattr(pipeline.llm, "generate", generate)
    blocks = await collect(pipeline.run(3, True, "폭언하는 고객 전화 먼저 끊어도 되나요"))
    text = "".join(b[1]["text"] for b in blocks if b[0] == "token")
    assert "따라서" not in text and "허용되지 않습니다" not in text
    assert "종료할 수 있습니다" in text and "\n\n보호담당자에게 알립니다" in text
    assert dict(blocks)["citations"]["unverified"] == []
