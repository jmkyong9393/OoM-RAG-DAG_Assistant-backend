"""답변 품질 채점(eval/answer_quality.py)의 계산부 — 대시보드에 기록하는 값. LLM·DB 불필요."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
import answer_quality as aq  # noqa: E402


def test_faithfulness_counts_only_supported_statements():
    v = [{"n": 1, "supported": True}, {"n": 2, "supported": False}, {"n": 3, "supported": True}]
    assert aq.faithfulness_of(v, 4) == 0.5, "판정이 빠진 문장(4번)은 뒷받침되지 않은 것으로 센다"
    assert aq.faithfulness_of([{"n": 1, "supported": "true"}], 1) == 0.0, "true 가 아닌 값은 인정하지 않는다"
    assert aq.faithfulness_of([], 0) is None, "사실 문장이 없으면 계산하지 않는다"


def test_relevance_is_zero_for_evasive_answers():
    assert aq.relevance_of([0.8, 0.6, 0.7], False) == 0.7
    assert aq.relevance_of([0.8, 0.6, 0.7], True) == 0.0
    assert aq.relevance_of([], False) == 0.0


def test_markers_are_removed_before_scoring():
    assert aq.strip_markers("힙은 70% 다 [P1]. 당시에는 75% 였다 [N3][P2].") == "힙은 70% 다. 당시에는 75% 였다."


def test_summarize_skips_unanswered_questions_for_faithfulness():
    rows = [{"faithfulness": 1.0, "answer_relevance": 0.8, "statements": 3, "supported": 3, "noncommittal": False, "no_answer": False},
            {"faithfulness": 0.5, "answer_relevance": 0.6, "statements": 2, "supported": 1, "noncommittal": False, "no_answer": False},
            {"faithfulness": None, "answer_relevance": 0.0, "statements": 0, "supported": 0, "noncommittal": True, "no_answer": True}]
    s = aq.summarize(rows)
    assert s["faithfulness"] == 0.75 and s["answer_relevance"] == round(1.4 / 3, 4) and s["n"] == 3
    assert s["meta"] == {"faithfulness_n": 2, "statements": 5, "supported": 4, "noncommittal": 1, "no_answer": 1}


def test_baseline_pairs_each_question_with_another_answers_questions():
    rows = [{"query": "a", "_q": [1.0, 0.0], "_g": [[1.0, 0.0]]}, {"query": "b", "_q": [0.0, 1.0], "_g": [[0.0, 1.0]]}]
    assert aq.baseline_of(rows) == 0.0, "서로 다른 질문끼리의 유사도"
    assert aq.baseline_of([rows[0], {**rows[0]}]) is None, "같은 질문끼리는 기준선에 넣지 않는다"
    assert round(aq.cosine([1.0, 1.0], [1.0, 0.0]), 4) == 0.7071
