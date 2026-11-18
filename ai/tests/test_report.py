"""장애 보고서(R1)의 AI 칸 — LLM 입력 만들기, 결과 검증(근거 번호 없는 문장은 버림), openai/키 없음, 워커 작업(DB)."""
import uuid

import pytest
from psycopg.types.json import Jsonb

from app import report
from app.config import profile
from app.llm import client as llm
from tests.conftest import requires_db
from tests.openai_stub import OpenAIStub

CONTENT = {
    "meta": {"title": "DB 커넥션 풀 고갈", "status": "resolved", "first_reported_at": "2026-07-10T08:00:00Z", "resolved_at": "2026-07-12T08:00:00Z"},
    "causes": [{"ref": "N2", "title": "풀 크기 10 고정", "link_reason": "풀이 작아 대기가 쌓였다", "explanation": None}],
    "resolution": {"items": [{"ref": "N3", "title": "풀 최대 20", "summary": "풀을 20 으로 늘려 해소"}], "resolve_note": None, "others": [], "note": None},
    "side_issues": [{"ref": "N4", "title": "DB 연결 한도 초과", "note": None}],
    "policy": [{"ref": "N3", "relation": "follows", "document": "배포 규정", "article": "제3조", "note": None}],
    "summary": [], "impact": [], "prevention": [], "open_items": [],
    "records": [{"ref": "N1", "id": 1, "kind": "occurrence", "title": "DB 커넥션 풀 고갈", "at": "2026-07-10T08:00:00Z"},
                {"ref": "N2", "id": 2, "kind": "cause", "title": "풀 크기 10 고정", "at": "2026-07-10T16:00:00Z"},
                {"ref": "N3", "id": 3, "kind": "resolution", "title": "풀 최대 20", "at": "2026-07-12T08:00:00Z"},
                {"ref": "N4", "id": 4, "kind": "side_issue", "title": "DB 연결 한도 초과", "at": "2026-07-20T08:00:00Z"}],
}
BODIES = {1: "피크 시간에 API 응답이\n지연됐다", 2: "풀 크기 10 고정", 3: "x" * 600}


def test_input_lists_records_with_kind_date_body_and_policy():
    lines = report.render(CONTENT, BODIES).split("\n")
    assert lines[0] == "[장애 건] DB 커넥션 풀 고갈 · 상태: 완료 · 최초 기록 07/10 · 완료 07/12"
    assert lines[1:5] == ["'원인' 기록 1건: N2", "'해결' 기록 1건: N3", "'추가 문제' 기록 1건: N4", "[기록]"]   # 빠뜨리면 안 되는 기록을 먼저 짚는다
    assert lines[5] == "N1 (발생, 07/10) DB 커넥션 풀 고갈 — 피크 시간에 API 응답이 지연됐다"            # 줄바꿈은 한 칸으로
    assert lines[6] == "N2 (원인, 07/11) 풀 크기 10 고정 ← 이어 붙인 이유: 풀이 작아 대기가 쌓였다"   # 한국 시간 날짜, 제목과 같은 본문은 생략
    assert lines[7] == f"N3 (해결, 07/12) 풀 최대 20 — {'x' * 500}… / 결과: 풀을 20 으로 늘려 해소"   # 본문은 앞 500자
    assert lines[8] == "N4 (추가 문제, 07/20) DB 연결 한도 초과"
    assert lines[9:] == ["[규정]", "N3 규정 따름: 배포 규정 제3조"]
    acted = {**CONTENT, "records": [*CONTENT["records"], {"ref": "N5", "id": 5, "kind": "action", "title": "풀 사용률 경보 추가", "at": "2026-07-21T08:00:00Z"}]}
    assert report.render(acted, BODIES).split("\n")[3] == "'조치' 기록 1건: N5"       # 재발 방지 칸에서 살펴볼 기록도 짚는다


def test_merge_keeps_only_sentences_with_known_refs():
    data = {"summary": {"problem": {"text": " 풀이 고갈됐습니다. ", "refs": ["N1", "N9", "N1"]}, "cause": {"text": "근거가 없습니다.", "refs": []}, "resolution": "문자열"},
            "impact": [{"text": "없는 번호만 있습니다.", "refs": ["N9"]}],
            "causes": {"N2": {"text": "풀이 작았습니다.", "refs": ["N2"]}, "N7": {"text": "없는 원인", "refs": ["N7"]}},
            "resolution_note": {"text": "풀을 늘려 해소했습니다.", "refs": ["N3"]},
            "side_issues": {"N4": {"text": "", "refs": ["N4"]}},
            "prevention": [{"text": "풀 사용률 경보를 추가했습니다.", "refs": ["N3"], "owner": "김담당", "due": " 2026-08-01 "}],
            "open_items": None}
    out = report.merge(CONTENT, data)
    assert out["summary"] == [{"text": "풀이 고갈됐습니다.", "refs": ["N1"]}]          # 없는 번호 · 중복 제거, 근거 없는 문장은 버림
    assert out["impact"] == [] and out["open_items"] == []
    kept = report.merge({**CONTENT, "open_items": [{"text": "DB 연결 한도 초과", "refs": ["N4"]}]}, {"open_items": [{"text": "AI 가 쓴 남은 일", "refs": ["N1"]}]})
    assert kept["open_items"] == [{"text": "DB 연결 한도 초과", "refs": ["N4"]}]          # 남은 확인은 사실 칸 — AI 가 써 보내도 받지 않는다
    assert out["causes"] == [{**CONTENT["causes"][0], "explanation": {"text": "풀이 작았습니다.", "refs": ["N2"]}}]
    assert out["resolution"]["note"] == {"text": "풀을 늘려 해소했습니다.", "refs": ["N3"]} and out["resolution"]["items"] == CONTENT["resolution"]["items"]
    assert out["side_issues"][0]["note"] is None
    own = report.merge(CONTENT, {"causes": {"N2": {"text": "풀이 작았습니다.", "refs": []}}, "side_issues": {"N4": {"text": "한도를 넘었습니다."}}})
    assert own["causes"][0]["explanation"]["refs"] == ["N2"] and own["side_issues"][0]["note"]["refs"] == ["N4"]   # 번호별 칸은 그 기록이 근거
    assert out["prevention"] == [{"text": "풀 사용률 경보를 추가했습니다.", "refs": ["N3"], "owner": "김담당", "due": "2026-08-01"}]
    assert out["records"] == CONTENT["records"] and CONTENT["summary"] == []          # 사실 칸과 원본은 그대로
    assert report.merge(CONTENT, data, external=True)["prevention"][0]["owner"] is None   # 외부판은 사람 이름을 싣지 않는다
    loose = report.merge(CONTENT, {"prevention": [{"text": "합산 규칙이 필요합니다.", "refs": ["N4"]}, {"text": "지침을 따릅니다.", "refs": ["N1"]}]})
    assert loose["prevention"] == []                                                  # '해결' · '조치' 기록이 근거가 아니면 재발 방지에 싣지 않는다
    assert report.merge(CONTENT, {})["summary"] == []                                   # 빈 응답이어도 형식은 유지


SUMMARY = {"problem": {"text": "풀이 고갈됐습니다.", "refs": ["N1"]}, "cause": {"text": "원인은 풀 크기 10 고정입니다.", "refs": ["N2"]},
           "resolution": {"text": "풀을 20 으로 늘려 해결했습니다.", "refs": ["N3"]}}


def test_summary_puts_problem_cause_resolution_in_order():
    out = report.merge(CONTENT, {"summary": SUMMARY, "resolution_note": {"text": "풀을 늘려 해소했습니다.", "refs": ["N3"]}})
    assert [s["refs"] for s in out["summary"]] == [["N1"], ["N2"], ["N3"]] and out["resolution"]["note"]["refs"] == ["N3"]


def test_summary_drops_cause_sentence_without_cause_records():
    """팀이 원인으로 표시한 기록이 없으면 AI 가 원인 문장을 줘도 싣지 않는다."""
    out = report.merge({**CONTENT, "causes": []}, {"summary": SUMMARY})
    assert [s["text"] for s in out["summary"]] == ["풀이 고갈됐습니다.", "풀을 20 으로 늘려 해결했습니다."]


@pytest.mark.parametrize(("status", "sentence"), [("open", report.NO_FIX_OPEN), ("resolved", report.NO_FIX_DONE)])
def test_summary_says_no_fix_without_resolution_records(status, sentence):
    """해결 기록이 없으면 AI 가 해결됐다고 써도 상태에 맞는 고정 문장으로 바꾸고 해결 요약은 비운다(완료된 건에는 '아직'을 쓰지 않는다)."""
    no_fix = {**CONTENT, "meta": {**CONTENT["meta"], "status": status}, "resolution": {**CONTENT["resolution"], "items": []}}
    out = report.merge(no_fix, {"summary": SUMMARY, "resolution_note": {"text": "풀을 늘려 해결됐습니다.", "refs": ["N3"]}})
    assert out["summary"][-1] == {"text": sentence, "refs": ["N1"]} and len(out["summary"]) == 3
    assert out["resolution"]["note"] is None


async def test_fill_uses_answer_model_and_validates_output(monkeypatch):
    with OpenAIStub() as stub:
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_ANSWER", "stub-answer")
        usage = llm.Usage()
        out = await report.fill(profile(), CONTENT, BODIES, True, usage)
        body = stub.calls[-1]["body"]
    assert body["model"] == "stub-answer" and body["response_format"] == {"type": "json_object"} and body["max_tokens"] == report.MAX_TOKENS
    assert "외부 공개용" in body["messages"][0]["content"] and body["messages"][1]["content"].startswith("[장애 건] DB 커넥션 풀 고갈")
    assert out["summary"] == [{"text": "스텁 보고서 요약입니다.", "refs": ["N1"]}]
    assert out["causes"][0]["explanation"] == {"text": "스텁 원인 설명입니다.", "refs": ["N2"]}
    assert out["prevention"] == [{"text": "스텁 재발 방지입니다.", "refs": ["N3"], "owner": None, "due": None}] and out["open_items"] == []
    assert (usage.prompt_tokens, usage.completion_tokens) == (42, 7)


def test_reuse_copies_internal_cells_into_external_facts():
    """외부용은 내부용 AI 칸을 옮겨 쓴다 — 담당자는 지우고, 기록이 달라졌거나 문장에 작성자 이름이 있으면 옮기지 않는다."""
    data = {"summary": {"problem": {"text": "풀이 고갈됐습니다.", "refs": ["N1"]}}, "causes": {"N2": {"text": "풀이 작았습니다.", "refs": ["N2"]}},
            "prevention": [{"text": "풀을 늘렸습니다.", "refs": ["N3"], "owner": "김담당", "due": None}]}
    named = lambda who: {**CONTENT, "records": [{**r, "author": who} for r in CONTENT["records"]]}  # noqa: E731
    src, ext = report.merge(named("김담당"), data), named(None)
    out = report.reuse(ext, src, BODIES)
    assert out["summary"] == src["summary"] and out["causes"][0]["explanation"] == src["causes"][0]["explanation"]
    assert out["prevention"] == [{"text": "풀을 늘렸습니다.", "refs": ["N3"], "owner": None, "due": None}]
    assert all(r["author"] is None for r in out["records"])                              # 사실 칸은 외부용 그대로
    assert report.reuse(ext, src, {**BODIES, 2: "풀 크기 20 고정"}) is not None          # 본문은 양쪽이 같은 값을 읽는다
    assert report.reuse({**ext, "meta": {**ext["meta"], "title": "다른 제목"}}, src, BODIES) is None   # LLM 입력이 다르면 새로 만든다
    leak = report.merge(named("김담당"), {"summary": {"problem": {"text": "김담당이 풀을 늘렸습니다.", "refs": ["N3"]}}})
    assert report.reuse(ext, leak, BODIES) is None                                       # 작성자 이름이 문장에 있으면 옮기지 않는다


async def test_fill_is_skipped_without_key(monkeypatch):
    monkeypatch.setenv("AI_MODE", "auto")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "")
    assert await report.fill(profile(), CONTENT, BODIES, False, llm.Usage()) is None


def _insert(admin_conn, content: dict, audience: str = "internal") -> int:
    root = admin_conn.execute("SELECT id, workspace_id, author_id FROM log_nodes ORDER BY id LIMIT 1").fetchone()
    return admin_conn.execute("""INSERT INTO incident_reports (workspace_id, root_id, audience, sig, content, requested_by)
                                 VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                              (root["workspace_id"], root["id"], audience, uuid.uuid4().hex, Jsonb(content), root["author_id"])).fetchone()["id"]


@requires_db
async def test_external_job_reuses_internal_cells_without_llm_call(monkeypatch, admin_conn):
    """내부용이 이미 있으면 외부용은 LLM 을 부르지 않고 그 문장을 옮겨 쓴다 — 토큰 0."""
    from app.retrieval import db

    async def never(*a, **k):
        raise AssertionError("LLM must not be called")

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", never)
    done = report.merge(CONTENT, {"summary": {"problem": {"text": "풀이 고갈됐습니다.", "refs": ["N1"]}}})
    a, b = _insert(admin_conn, done), _insert(admin_conn, CONTENT, "external")
    admin_conn.execute("UPDATE incident_reports SET status = 'done', prompt_version = %s, model = 'stub-answer' WHERE id = %s", (report.PROMPT_VERSION, a))
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 1, "report_failed": 0}
        row = admin_conn.execute("SELECT status, model, prompt_tokens, content FROM incident_reports WHERE id = %s", (b,)).fetchone()
        assert row["status"] == "done" and row["model"] == "stub-answer" and row["prompt_tokens"] == 0
        assert row["content"]["summary"] == [{"text": "풀이 고갈됐습니다.", "refs": ["N1"]}]
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([a, b],))


@requires_db
async def test_job_finishes_without_ai_when_key_is_missing(monkeypatch, admin_conn):
    """키가 없으면(fake) AI 칸 없이 done — 사실 칸은 그대로, 지시문 판 · 모델은 비어 있다. 같은 공개 범위의 이전 보고서는 지운다."""
    from app.retrieval import db
    monkeypatch.setenv("AI_MODE", "fake")
    old = _insert(admin_conn, CONTENT)
    admin_conn.execute("UPDATE incident_reports SET status = 'done' WHERE id = %s", (old,))
    rid = _insert(admin_conn, CONTENT)
    try:
        with db.connect() as conn:
            await report.run_pending(conn)
        row = admin_conn.execute("SELECT status, prompt_version, model, generated_at, content FROM incident_reports WHERE id = %s", (rid,)).fetchone()
        assert row["status"] == "done" and row["prompt_version"] is None and row["model"] is None and row["generated_at"]
        assert row["content"] == CONTENT
        assert admin_conn.execute("SELECT count(*) AS n FROM incident_reports WHERE id = %s", (old,)).fetchone()["n"] == 0
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([old, rid],))


@requires_db
async def test_job_marks_failed_and_keeps_facts(monkeypatch, admin_conn):
    """LLM 이 형식에 안 맞는 답을 내면 failed — 오류를 남기고 사실 칸은 지우지 않는다. 다음 주기에 다시 집지 않는다."""
    from app.retrieval import db

    async def broken(*a, **k):
        return ["not", "an", "object"]

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", broken)
    rid = _insert(admin_conn, CONTENT)
    try:
        with db.connect() as conn:
            await report.run_pending(conn)
            again = await report.run_pending(conn)
        row = admin_conn.execute("SELECT status, error, content FROM incident_reports WHERE id = %s", (rid,)).fetchone()
        assert row["status"] == "failed" and "AI_BAD_OUTPUT" in row["error"] and row["content"] == CONTENT
        assert again == {"reports": 0, "report_failed": 0}
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = %s", (rid,))


@requires_db
@pytest.mark.parametrize("answer", [{}, {"summary": {"problem": {"text": "근거 번호가 없는 문장입니다.", "refs": []}}, "impact": [{"text": "없는 번호입니다.", "refs": ["N9"]}]},
                                    {"summary": [{"text": "옛 모양(배열)의 한눈에 보기입니다.", "refs": ["N1"]}]}])
async def test_job_fails_on_empty_output_and_keeps_previous_report(monkeypatch, admin_conn, answer):
    """AI 가 빈 결과(또는 근거 번호 없는 문장뿐, 옛 모양의 한눈에 보기)를 내면 failed — 문장 없는 보고서가 멀쩡한 이전 보고서를 지우지 않는다."""
    from app.retrieval import db

    async def empty(*a, **k):
        return answer

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", empty)
    old = _insert(admin_conn, report.merge(CONTENT, {"summary": {"problem": {"text": "풀이 고갈됐습니다.", "refs": ["N1"]}}}))
    admin_conn.execute("UPDATE incident_reports SET status = 'done' WHERE id = %s", (old,))
    rid = _insert(admin_conn, CONTENT)
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 0, "report_failed": 1}
        row = admin_conn.execute("SELECT status, error, content FROM incident_reports WHERE id = %s", (rid,)).fetchone()
        assert row["status"] == "failed" and "AI_EMPTY_OUTPUT" in row["error"] and row["content"] == CONTENT
        assert admin_conn.execute("SELECT status FROM incident_reports WHERE id = %s", (old,)).fetchone()["status"] == "done"
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([old, rid],))


def _age(admin_conn, rid: int, minutes: int) -> None:
    admin_conn.execute("UPDATE incident_reports SET created_at = now() - make_interval(mins => %s) WHERE id = %s", (minutes, rid))


@requires_db
async def test_stuck_request_is_failed_without_calling_llm(monkeypatch, admin_conn):
    """10분 넘게 generating 인 요청은 화면에 이미 실패로 보였다 — 뒤늦게 켜진 워커가 AI 를 부르지 않고 failed 로 바꾼다."""
    from app.retrieval import db

    async def never(*a, **k):
        raise AssertionError("LLM must not be called")

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", never)
    rid = _insert(admin_conn, CONTENT)
    _age(admin_conn, rid, 11)
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 0, "report_failed": 0}
        row = admin_conn.execute("SELECT status, error, generated_at, content FROM incident_reports WHERE id = %s", (rid,)).fetchone()
        assert row["status"] == "failed" and row["error"].startswith("AI_TIMEOUT:") and row["generated_at"] and row["content"] == CONTENT
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = %s", (rid,))


@requires_db
async def test_fresh_request_is_still_processed_next_to_a_stuck_one(monkeypatch, admin_conn):
    from app.retrieval import db
    monkeypatch.setenv("AI_MODE", "fake")
    stuck, fresh = _insert(admin_conn, CONTENT, "external"), _insert(admin_conn, CONTENT)
    _age(admin_conn, stuck, 11)
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 1, "report_failed": 0}
        status = lambda rid: admin_conn.execute("SELECT status FROM incident_reports WHERE id = %s", (rid,)).fetchone()["status"]  # noqa: E731
        assert (status(stuck), status(fresh)) == ("failed", "done")
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([stuck, fresh],))


@requires_db
async def test_stuck_request_does_not_remove_previous_report(monkeypatch, admin_conn):
    """멈춘 요청이 failed 로 바뀌어도 이전 완료본은 남는다(이전 것은 새 보고서가 done 이 될 때만 지운다)."""
    from app.retrieval import db
    monkeypatch.setenv("AI_MODE", "fake")
    old = _insert(admin_conn, CONTENT)
    admin_conn.execute("UPDATE incident_reports SET status = 'done' WHERE id = %s", (old,))
    stuck = _insert(admin_conn, CONTENT)
    _age(admin_conn, stuck, 11)
    try:
        with db.connect() as conn:
            await report.run_pending(conn)
        rows = {r["id"]: r["status"] for r in admin_conn.execute("SELECT id, status FROM incident_reports WHERE id = ANY(%s)", ([old, stuck],)).fetchall()}
        assert rows == {old: "done", stuck: "failed"}
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([old, stuck],))


def test_check_script_counts_missing_causes_and_fixes():
    """eval/report_checks.py 의 계산 — 한눈에 보기가 언급한 원인 · 해결을 근거 번호와 제목 낱말로 센다."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
    import report_checks as rc
    data = {"summary": {"problem": {"text": "풀이 고갈됐습니다. 풀 크기 10 고정이 원인이었습니다.", "refs": ["N1"]}},
            "causes": {"N2": {"text": "풀이 작았습니다.", "refs": ["N2"]}}}
    s = rc.score(report.merge(CONTENT, data))
    assert s["causes"] == 1 and s["causes_explained"] == 1 and s["fixes"] == 1 and s["side_issues_noted"] == 0
    assert s["causes_missing_by_ref"] == ["N2"] and s["causes_missing_by_text"] == []     # 번호는 없지만 제목 낱말이 글에 있다
    assert s["fixes_missing_by_ref"] == ["N3"] and s["fixes_missing_by_text"] == ["N3"]   # 해결 기록은 어디에도 없다
    assert s["multi_sentence_items"] == 1 and s["resolution_note"] is False
    assert (s["cause_without_record"], s["fix_without_record"]) == (0, 0)
    stale = {**CONTENT, "causes": [], "resolution": {**CONTENT["resolution"], "items": []},   # 옛 판이 만든 모양 — 기록에 없는 원인 · 해결 문장
             "summary": [{"text": "문제입니다.", "refs": ["N1"]}, {"text": "원인은 풀입니다.", "refs": ["N2"]}, {"text": "해결됐습니다.", "refs": ["N3"]}]}
    assert (rc.score(stale)["cause_without_record"], rc.score(stale)["fix_without_record"]) == (1, 1)
    opened = rc.without_fixes(CONTENT)   # 진행 중 표본 — 해결 기록 지정을 뺀 사본
    assert opened["resolution"]["items"] == [] and opened["records"][2]["kind"] == "action" and CONTENT["records"][2]["kind"] == "resolution"
    assert report.merge(opened, {"summary": SUMMARY})["summary"][-1]["text"] == report.NO_FIX_OPEN
    t = rc.totals([{"score": s, "prompt_tokens": 10, "completion_tokens": 5}, {"error": "x"}])
    assert (t["reports"], t["failed"], t["full_by_text"], t["fixes_missing_by_text"], t["prompt_tokens"]) == (2, 1, 0, 1, 10)
    blank = rc.facts_only(report.merge(CONTENT, data))
    assert blank["summary"] == [] and blank["causes"][0]["explanation"] is None and blank["records"] == CONTENT["records"]
