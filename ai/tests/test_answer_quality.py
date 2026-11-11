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


def test_only_the_exact_server_notice_at_the_end_is_left_out_of_scoring():
    notice = "※ 급여·근로시간 기준은 「급여 규정」도 함께 확인해야 합니다."
    body = "응대를 중단한 근로자는 30분 이상의 유급 안정시간을 가진다."
    assert aq.split_notice(f"{body}\n\n{notice}", notice) == (body, [notice])
    assert aq.split_notice(f"{body}\n\n{notice}\n", notice) == (body, [notice]), "끝 공백은 무시"
    # 모델이 스스로 쓴 '※' 문장은 채점 대상으로 남는다
    own = f"{body}\n※ 위반 시 과태료 500만 원이 부과됩니다."
    assert aq.split_notice(own, None) == (own, [])
    assert aq.split_notice(own, notice) == (own, []), "서버 안내가 아니면 떼지 않는다"
    # 서버 안내와 같은 글자가 답변 중간에 있으면 떼지 않는다(맨 끝만)
    mid = f"{notice}\n{body}"
    assert aq.split_notice(mid, notice) == (mid, [])
    assert aq.split_notice(body, None) == (body, [])


def test_notice_after_a_truncated_answer_is_still_split():
    notice = "※ 정보보안 기준은 「정보보안 기본규정」도 함께 확인해야 합니다."
    cut = "외부 반출은 부서장 승인을 받는다.\n(답변 길이 상한에 도달해 이하 생략)"
    assert aq.split_notice(f"{cut}\n\n{notice}", notice) == (cut, [notice])


def test_schema_check_stops_on_an_older_database(tmp_path):
    import schema_check as sc
    for n in ("V1__init.sql", "V9__x.sql", "V14__policy_uploads.sql", "R__functions.sql"):
        (tmp_path / n).write_text("", encoding="utf-8")
    assert sc.repo_version(tmp_path) == 14, "문자열이 아니라 숫자로 비교한다(V9 < V14)"
    assert sc.repo_version() >= 14
    assert sc.stale_message(14, 14, "oom") is None and sc.stale_message(15, 14, "oom") is None
    msg = sc.stale_message(11, 14, "oom_eval")
    assert "V11" in msg and "V14" in msg and "POSTGRES_DB=oom_eval" in msg
