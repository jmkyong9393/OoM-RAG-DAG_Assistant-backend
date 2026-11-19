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


def test_resolution_note_needs_a_fix_record_among_its_refs():
    """해결 요약의 근거에 해결 기록이 하나도 없으면 싣지 않는다. 해결 기록 일부만 근거로 댄 것은 둔다."""
    note = lambda refs: report.merge(TWO_FIXES, {"summary": SUMMARY, "resolution_note": {"text": "지연이 사라졌습니다.", "refs": refs}})["resolution"]["note"]  # noqa: E731
    assert note(["N2"]) is None and note(["N1", "N4"]) is None
    assert note(["N3"])["refs"] == ["N3"] and note(["N2", "N5"])["refs"] == ["N2", "N5"]


async def test_fill_uses_answer_model_and_validates_output(monkeypatch):
    with OpenAIStub() as stub:
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_MODEL_ANSWER", "stub-answer")
        usage = llm.Usage()
        out = await report.fill(profile(), CONTENT, BODIES, True, usage)
        body = stub.calls[-1]["body"]
    fmt = body["response_format"]
    assert body["model"] == "stub-answer" and fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True and body["max_tokens"] == report.MAX_TOKENS
    schema = fmt["json_schema"]["schema"]["properties"]
    assert list(schema["causes"]["properties"]) == ["N2"] and list(schema["side_issues"]["properties"]) == ["N4"]          # 키는 이 장애 건의 기록 번호
    assert schema["impact"]["items"]["properties"]["refs"]["items"]["enum"] == ["N1", "N2", "N3", "N4"]                       # 근거는 [기록]의 번호만
    assert schema["prevention"]["items"]["properties"]["refs"]["items"]["enum"] == ["N1", "N2", "N3", "N4"]                    # 재발 방지의 근거도 좁히지 않는다 — 받은 뒤 버린다
    assert "외부 공개용" in body["messages"][0]["content"] and body["messages"][1]["content"].startswith("[장애 건] DB 커넥션 풀 고갈")
    assert out["summary"] == [{"text": "스텁 보고서 요약입니다.", "refs": ["N1"]}]
    assert out["causes"][0]["explanation"] == {"text": "스텁 원인 설명입니다.", "refs": ["N2"]}
    assert out["prevention"] == [{"text": "풀을 20 으로 늘렸습니다.", "refs": ["N3"], "owner": None, "due": None}] and out["open_items"] == []
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


TWO_FIXES = {**CONTENT,
             "resolution": {**CONTENT["resolution"], "items": [*CONTENT["resolution"]["items"], {"ref": "N5", "title": "타임아웃 3초", "summary": None}]},
             "records": [*CONTENT["records"], {"ref": "N5", "id": 5, "kind": "resolution", "title": "타임아웃 3초", "at": "2026-07-13T08:00:00Z"}]}
BOTH_FIXES = {"text": "풀을 20 으로 늘리고 타임아웃을 3초로 줄였습니다.", "refs": ["N5", "N3"]}


def _llm(monkeypatch, main, small: dict | None = None) -> list[dict]:
    """본 호출에는 main(목록이면 부를 때마다 다음 것, 끝나면 마지막 것), 작은 호출에는 small[그 지시문]을 돌려주는 가짜 LLM(값이 예외면 던진다).
    부른 호출을 순서대로 남긴다."""
    calls: list[dict] = []
    mains = list(main) if isinstance(main, list) else [main]

    async def chat(profile, model, system, user, usage, max_tokens=None, schema=None):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        usage.prompt_tokens += 10
        usage.completion_tokens += 2
        if system.startswith(report.SYSTEM):
            answer = mains.pop(0) if len(mains) > 1 else mains[0]
        else:
            answer = next(v for k, v in (small or {}).items() if system.startswith(k))   # 예상하지 않은 작은 호출이면 StopIteration 으로 드러난다
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", chat)
    return calls


# 번호별 칸(원인 설명 · 추가 문제 메모)까지 채운 본 호출 응답 — 이것이 빠지면 본 호출을 한 번 더 부른다
NOTES = {"causes": {"N2": {"text": "풀이 작아 대기가 쌓였습니다.", "refs": ["N2"]}}, "side_issues": {"N4": {"text": "연결 한도를 넘었습니다.", "refs": ["N4"]}}}


def _summary(fix) -> dict:
    return {"summary": {**SUMMARY, "resolution": fix}, **NOTES}


async def test_fix_sentence_that_cites_every_fix_is_not_refetched(monkeypatch):
    calls = _llm(monkeypatch, _summary({"text": "풀을 늘리고 타임아웃을 줄였습니다.", "refs": ["N3", "N5"]}))
    out = await report.fill(profile(), TWO_FIXES, BODIES, False, llm.Usage())
    assert len(calls) == 1 and out["summary"][-1]["refs"] == ["N3", "N5"]


async def test_missing_fix_is_refetched_from_fix_records_only(monkeypatch):
    """해결 기록 하나가 근거 번호에서 빠지면 해결 기록 줄만 주고 그 문장만 다시 받는다 — 다른 문장은 그대로, 토큰은 더한다."""
    calls = _llm(monkeypatch, _summary(SUMMARY["resolution"]), {report.SYSTEM_FIX: BOTH_FIXES})
    usage, trace = llm.Usage(), {}
    out = await report.fill(profile(), TWO_FIXES, BODIES, False, usage, trace)
    assert [s["refs"] for s in out["summary"]] == [["N1"], ["N2"], ["N3", "N5"]] and out["summary"][-1]["text"] == BOTH_FIXES["text"]
    assert calls[1]["system"] == report.SYSTEM_FIX and calls[1]["max_tokens"] == report.FIX_MAX_TOKENS
    assert calls[1]["user"] == f"N3 풀 최대 20 — {'x' * 500}… / 결과: 풀을 20 으로 늘려 해소\nN5 타임아웃 3초"   # 구분 · 날짜 없이 해결 기록만
    assert (usage.prompt_tokens, usage.completion_tokens) == (20, 4)
    assert trace["fix"]["accepted"] is True and trace["fix"]["before"]["refs"] == ["N3"]


@pytest.mark.parametrize("answer", [{"text": "풀을 20 으로 늘렸습니다.", "refs": ["N3"]},                       # 다시 받은 문장도 하나를 빠뜨림
                                    {**BOTH_FIXES, "refs": ["N3", "N5", "N2"]},                              # 해결 기록이 아닌 번호가 섞임
                                    {**BOTH_FIXES, "text": "풀을 20 으로 늘리고 타임아웃을 3초로 줄였음."},       # '니다'로 끝나지 않음
                                    {**BOTH_FIXES, "text": "가" * report.SENTENCE_MAX + "했습니다."},           # 길이 상한 초과
                                    ["not", "an", "object"], RuntimeError("timeout")])                        # 형식 오류 · 호출 실패
async def test_refetched_fix_that_fails_a_condition_keeps_the_original(monkeypatch, answer):
    calls = _llm(monkeypatch, _summary(SUMMARY["resolution"]), {report.SYSTEM_FIX: answer})
    trace = {}
    out = await report.fill(profile(), TWO_FIXES, BODIES, False, llm.Usage(), trace)
    assert len(calls) == 2 and out["summary"][-1] == SUMMARY["resolution"] and trace["fix"]["accepted"] is False   # 보고서는 그대로 끝난다


async def test_single_fix_is_never_refetched(monkeypatch):
    calls = _llm(monkeypatch, _summary({"text": "문제를 해결했습니다.", "refs": ["N1"]}))
    out = await report.fill(profile(), CONTENT, BODIES, False, llm.Usage())
    assert len(calls) == 1 and out["summary"][-1]["refs"] == ["N1"]


async def test_absent_fix_sentence_is_refetched_and_appended(monkeypatch):
    """해결 기록이 둘인데 해결 문장을 아예 쓰지 않았으면 다시 받아 끝에 붙인다(원인 문장을 덮어쓰지 않는다). 외부용이면 외부용 규칙이 붙는다."""
    calls = _llm(monkeypatch, _summary(None), {report.SYSTEM_FIX: BOTH_FIXES})
    out = await report.fill(profile(), TWO_FIXES, BODIES, True, llm.Usage())
    assert [s["refs"] for s in out["summary"]] == [["N1"], ["N2"], ["N3", "N5"]]
    assert calls[1]["system"] == report.SYSTEM_FIX + report.EXTERNAL_RULE


async def test_empty_main_output_is_not_patched_by_a_small_call(monkeypatch):
    """한눈에 보기가 비면 빈 결과다 — 작은 호출로 문장을 채워 실패를 가리지 않는다."""
    calls = _llm(monkeypatch, {"summary": {"resolution": SUMMARY["resolution"]}}, {report.SYSTEM_FIX: BOTH_FIXES})
    out = await report.fill(profile(), TWO_FIXES, BODIES, False, llm.Usage())
    assert len(calls) == 1 and out["summary"] == []


ACTED = {**CONTENT, "records": [*CONTENT["records"],
                                {"ref": "N5", "id": 5, "kind": "action", "title": "풀 사용률 경보 추가", "at": "2026-07-13T08:00:00Z"},
                                {"ref": "N6", "id": 6, "kind": "action", "title": "고객 공지 발송", "at": "2026-07-14T08:00:00Z"},
                                {"ref": "N7", "id": 7, "kind": "action", "title": "런북 갱신", "at": "2026-07-15T08:00:00Z"}]}
ACTED_BODIES = {**BODIES, 3: "풀 최대를 20 으로 조정했다", 5: "풀 사용률 80% 경보를 추가했다", 6: "지연을 겪은 고객에게 사과 공지를 발송했다", 7: "풀 고갈 대응 런북을 갱신했다"}
ALARM = {"text": "풀 사용률 80% 경보를 추가했습니다.", "refs": ["N5"]}
NOTICE = {"text": "지연을 겪은 고객에게 사과 공지를 발송했습니다.", "refs": ["N6"]}
RUNBOOK = {"text": "풀 고갈 대응 런북을 갱신했습니다.", "refs": ["N7"]}
ACTION_LINES = ["N5 (조치) 풀 사용률 경보 추가 — 풀 사용률 80% 경보를 추가했다", "N6 (조치) 고객 공지 발송 — 지연을 겪은 고객에게 사과 공지를 발송했다",
                "N7 (조치) 런북 갱신 — 풀 고갈 대응 런북을 갱신했다"]


def _main(*prevention) -> dict:
    return {"summary": SUMMARY, "prevention": list(prevention), **NOTES}


async def _fill(content=ACTED, bodies=ACTED_BODIES, external=False):
    usage, trace = llm.Usage(), {}
    return await report.fill(profile(), content, bodies, external, usage, trace), usage, trace


async def test_prevention_is_not_supplemented_when_main_call_cited_every_candidate(monkeypatch):
    """조치 기록을 본 호출이 모두 근거로 썼고 해결 기록에 고르는 낱말이 없으면 보완 호출을 부르지 않는다."""
    calls = _llm(monkeypatch, _main(ALARM, NOTICE, RUNBOOK))
    out, _, trace = await _fill()
    assert len(calls) == 1 and len(out["prevention"]) == 3 and "prevention" not in trace


async def test_prevention_is_supplemented_from_records_the_main_call_left_out(monkeypatch):
    """본 호출이 안 쓴 조치 기록만 주고 받은 문장을 뒤에 붙인다 — 토큰은 더하고, 담당자 · 기한은 비운다."""
    calls = _llm(monkeypatch, _main({**ALARM, "owner": "김담당", "due": "2026-08-01"}), {report.SYSTEM_PREVENTION: {"prevention": [NOTICE]}})
    out, usage, trace = await _fill()
    assert out["prevention"] == [{**ALARM, "owner": "김담당", "due": "2026-08-01"}, {**NOTICE, "owner": None, "due": None}]
    assert calls[1]["system"] == report.SYSTEM_PREVENTION and calls[1]["max_tokens"] == report.PREVENTION_MAX_TOKENS
    assert calls[1]["user"] == "\n".join(["[기록]", *ACTION_LINES[1:]])   # 이미 근거로 쓴 N5 · 낱말이 없는 해결 기록 N3 · 다른 칸의 문장은 넣지 않는다
    assert (usage.prompt_tokens, usage.completion_tokens) == (20, 4)
    assert trace["prevention"]["given"] == ["N6", "N7"] and trace["prevention"]["added"] == [out["prevention"][1]]


async def test_prevention_is_not_supplemented_without_fix_records(monkeypatch):
    """해결 기록이 없는 장애 건의 조치 기록은 대응 그 자체다 — 재발 방지로 싣지 않는다."""
    open_case = {**ACTED, "resolution": {**ACTED["resolution"], "items": []}, "records": [{**r, "kind": "action"} if r["ref"] == "N3" else r for r in ACTED["records"]]}
    calls = _llm(monkeypatch, _main())
    out, _, _ = await _fill(open_case)
    assert len(calls) == 1 and out["prevention"] == []


@pytest.mark.parametrize(("body", "given"), [("풀 최대를 20 으로 조정했다", False), ("풀 최대를 20 으로 조정하고 배포 점검표에 풀 설정 검증을 넣었다", True)])
async def test_fix_record_is_given_only_when_it_has_a_signal_word(monkeypatch, body, given):
    calls = _llm(monkeypatch, _main(), {report.SYSTEM_PREVENTION: {"prevention": []}})
    await _fill(bodies={**ACTED_BODIES, 3: body})
    assert calls[1]["user"].split("\n")[1:] == ([f"N3 (해결) 풀 최대 20 — {body} / 결과: 풀을 20 으로 늘려 해소"] if given else []) + ACTION_LINES


@pytest.mark.parametrize(("answer", "why"), [
    ([{"text": "풀을 20 으로 늘렸습니다.", "refs": ["N3"]}], "입력에 없는 근거 번호"),                 # 넘기지 않은 기록의 번호
    ([ALARM, {"text": "풀 사용률 경보는 80% 입니다.", "refs": ["N5"]}], "같은 기록의 둘째 문장"),
    ([{"text": "풀 사용률 80% 경보 추가.", "refs": ["N5"]}], "'니다'로 끝나지 않음"),                  # 기록의 메모 말투 그대로
    ([{"text": "풀 사용률 경보를 추가해야 합니다.", "refs": ["N5"]}], "권고 · 예정 · 해결 되풀이로 끝남"),
    ([{"text": "풀 사용률 경보 절차를 마련했습니다.", "refs": ["N5"]}], "기록에 없는 낱말"),
    ([{"text": "배포 파이프라인 승인 단계를 늘렸습니다.", "refs": ["N5"]}], "근거 기록과 겹침 60% 미만")])   # 다른 내용에 번호만 붙임
async def test_supplemented_sentence_that_fails_a_check_is_dropped(monkeypatch, answer, why):
    _llm(monkeypatch, _main(), {report.SYSTEM_PREVENTION: {"prevention": answer}})
    out, _, trace = await _fill()
    assert out["prevention"] == ([{**ALARM, "owner": None, "due": None}] if answer[0] == ALARM else [])
    assert trace["prevention"]["dropped"] == [[why, answer[-1]["text"]]]


async def test_supplemented_sentences_stop_at_the_list_limit(monkeypatch):
    four = [{"text": f"풀 최대를 {n}차에 20 으로 조정했습니다.", "refs": ["N3"]} for n in range(1, 5)]   # 근거 기록(N3)의 내용이어야 실린다
    _llm(monkeypatch, _main(*four), {report.SYSTEM_PREVENTION: {"prevention": [ALARM, NOTICE, RUNBOOK]}})
    out, _, _ = await _fill()
    assert [s["text"] for s in out["prevention"]] == [*(s["text"] for s in four), ALARM["text"]] and len(out["prevention"]) == report.LIST_MAX


async def test_external_supplement_carries_the_external_rule(monkeypatch):
    calls = _llm(monkeypatch, _main(), {report.SYSTEM_PREVENTION: {"prevention": [{**ALARM, "owner": "김담당"}]}})
    out, _, _ = await _fill(external=True)
    assert calls[1]["system"] == report.SYSTEM_PREVENTION + report.EXTERNAL_RULE and out["prevention"] == [{**ALARM, "owner": None, "due": None}]


@pytest.mark.parametrize("answer", [RuntimeError("timeout"), ["not", "an", "object"]])
async def test_failed_supplement_keeps_the_main_result(monkeypatch, answer):
    _llm(monkeypatch, _main(ALARM), {report.SYSTEM_PREVENTION: answer})
    out, _, trace = await _fill()
    assert out["prevention"] == [{**ALARM, "owner": None, "due": None}] and trace["prevention"]["added"] == []   # 보고서는 그대로 끝난다


async def test_both_small_calls_run_when_both_cells_fall_short(monkeypatch):
    """해결 문장 다시 받기와 재발 방지 보완은 서로 다른 칸이다 — 둘 다 걸리면 둘 다 부른다."""
    two = {**ACTED, "resolution": TWO_FIXES["resolution"],
           "records": [*ACTED["records"], {"ref": "N8", "id": 8, "kind": "resolution", "title": "타임아웃 3초", "at": "2026-07-16T08:00:00Z"}]}
    two["resolution"] = {**two["resolution"], "items": [two["resolution"]["items"][0], {"ref": "N8", "title": "타임아웃 3초", "summary": None}]}
    calls = _llm(monkeypatch, _main(), {report.SYSTEM_FIX: {**BOTH_FIXES, "refs": ["N3", "N8"]}, report.SYSTEM_PREVENTION: {"prevention": [NOTICE]}})
    out, usage, _ = await _fill(two)
    assert len(calls) == 3 and out["summary"][-1]["refs"] == ["N3", "N8"] and [s["refs"] for s in out["prevention"]] == [["N6"]]
    assert usage.prompt_tokens == 30


WRONG_KEY = {"summary": SUMMARY, "impact": [{"text": "응답이 지연됐습니다.", "refs": ["N1"]}], "causes": NOTES["causes"],
             "side_issues": {"N6": {"text": "예시 번호를 그대로 쓴 메모입니다.", "refs": ["N5"]}}}   # 지시문의 예시 번호를 키로 쓴 응답 — N4 의 메모가 빠진다


async def test_missing_note_is_refilled_by_calling_the_main_prompt_again(monkeypatch):
    """추가 문제 메모가 빠지면 본 호출을 같은 입력으로 한 번 더 불러 빠진 칸만 채운다 — 다른 칸은 첫 응답 그대로, 토큰은 더한다."""
    second = {"summary": {"problem": {"text": "다르게 쓴 문제입니다.", "refs": ["N1"]}}, "impact": [],
              "causes": {"N2": {"text": "다르게 쓴 원인 설명입니다.", "refs": ["N2"]}}, "side_issues": NOTES["side_issues"]}
    calls = _llm(monkeypatch, [WRONG_KEY, second])
    usage, trace = llm.Usage(), {}
    out = await report.fill(profile(), CONTENT, BODIES, True, usage, trace)
    assert len(calls) == 2 and calls[1] == calls[0] and calls[0]["system"] == report.SYSTEM + report.EXTERNAL_RULE   # 같은 지시문 · 같은 입력
    assert out["side_issues"][0]["note"] == NOTES["side_issues"]["N4"]
    assert out["summary"][0]["text"] == SUMMARY["problem"]["text"] and out["impact"] == WRONG_KEY["impact"]        # 첫 응답의 문장은 그대로
    assert out["causes"][0]["explanation"] == NOTES["causes"]["N2"]                                                 # 이미 있던 칸도 그대로
    assert (usage.prompt_tokens, usage.completion_tokens) == (20, 4) and trace["notes"] == {"missing": ["N4"], "filled": ["N4"]}


async def test_missing_cause_explanation_is_refilled_too(monkeypatch):
    calls = _llm(monkeypatch, [{"summary": SUMMARY, "side_issues": NOTES["side_issues"]}, {"summary": SUMMARY, **NOTES}])
    out = await report.fill(profile(), CONTENT, BODIES, False, llm.Usage())
    assert len(calls) == 2 and out["causes"][0]["explanation"] == NOTES["causes"]["N2"] and out["side_issues"][0]["note"] == NOTES["side_issues"]["N4"]


@pytest.mark.parametrize("second", [WRONG_KEY, ["not", "an", "object"], RuntimeError("timeout")])
async def test_note_stays_empty_when_the_second_call_does_not_help(monkeypatch, second):
    """다시 불러도 빠지거나 그 호출이 실패하면 빈 칸으로 둔다 — 한 번만 다시 부르고, 보고서는 그대로 끝난다."""
    calls = _llm(monkeypatch, [WRONG_KEY, second])
    trace = {}
    out = await report.fill(profile(), CONTENT, BODIES, False, llm.Usage(), trace)
    assert len(calls) == 2 and out["side_issues"][0]["note"] is None and out["summary"][0]["text"] == SUMMARY["problem"]["text"]
    assert trace["notes"]["filled"] == []


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
    note = lambda refs: rc.score({**TWO_FIXES, "resolution": {**TWO_FIXES["resolution"], "note": {"text": "지연이 사라졌습니다.", "refs": refs}}})  # noqa: E731
    assert (s["note_without_fix_ref"], s["multi_fix"], s["note_partial_fix_refs"]) == (0, 0, 0)                 # 해결 요약이 없으면 세지 않는다
    assert (note(["N2"])["note_without_fix_ref"], note(["N3"])["note_partial_fix_refs"], note(["N3", "N5"])["note_partial_fix_refs"]) == (1, 1, 0)
    memo = report.merge(CONTENT, {**data, "impact": [{"text": "결제 안 됨.", "refs": ["N1"]}], "causes": {"N2": {"text": "풀 크기 10 고정", "refs": ["N2"]}}})
    assert (s["not_polite_ending"], rc.score(memo)["not_polite_ending"]) == (0, 2)   # 메모 말투 · 명사로 끝난 문장
    stale = {**CONTENT, "causes": [], "resolution": {**CONTENT["resolution"], "items": []},   # 옛 판이 만든 모양 — 기록에 없는 원인 · 해결 문장
             "summary": [{"text": "문제입니다.", "refs": ["N1"]}, {"text": "원인은 풀입니다.", "refs": ["N2"]}, {"text": "해결됐습니다.", "refs": ["N3"]}]}
    assert (rc.score(stale)["cause_without_record"], rc.score(stale)["fix_without_record"]) == (1, 1)
    opened = rc.without_fixes(CONTENT)   # 진행 중 표본 — 해결 기록 지정을 뺀 사본
    assert opened["resolution"]["items"] == [] and opened["records"][2]["kind"] == "action" and CONTENT["records"][2]["kind"] == "resolution"
    assert report.merge(opened, {"summary": SUMMARY})["summary"][-1]["text"] == report.NO_FIX_OPEN
    t = rc.totals([{"score": s, "prompt_tokens": 10, "completion_tokens": 5}, {"error": "x"}])
    assert (t["reports"], t["failed"], t["full_by_text"], t["fixes_missing_by_text"], t["prompt_tokens"]) == (2, 1, 0, 1, 10)
    assert (t["fix_refetched"], t["prevention_supplemented"], t["prevention_added"], t["prevention_dropped"]) == (0, 0, 0, {})   # 작은 호출 기록이 없는 행
    small = rc.totals([{"score": s, "small_calls": {"fix": {"accepted": True}, "prevention": {"added": [1, 2], "dropped": [["기록에 없는 낱말", "…"]]}}},
                       {"score": s, "small_calls": {"prevention": {"added": [], "dropped": [["기록에 없는 낱말", "…"], ["같은 기록의 둘째 문장", "…"]], "error": "x"}}},
                       {"score": s, "small_calls": {"notes": {"missing": ["N4", "N5"], "filled": ["N4"]}}}])
    assert (small["fix_refetched"], small["fix_refetch_accepted"], small["prevention_supplemented"], small["prevention_added"]) == (1, 1, 2, 2)
    assert small["prevention_dropped"] == {"기록에 없는 낱말": 2, "같은 기록의 둘째 문장": 1} and small["small_call_errors"] == 1
    assert (small["notes_refilled_reports"], small["notes_missing"], small["notes_filled"], t["notes_refilled_reports"]) == (1, 2, 1, 0)
    blank = rc.facts_only(report.merge(CONTENT, data))
    assert blank["summary"] == [] and blank["causes"][0]["explanation"] is None and blank["records"] == CONTENT["records"]


async def test_main_call_prevention_that_does_not_match_its_record_is_dropped_and_the_record_is_asked_again(monkeypatch):
    """본 호출의 재발 방지 문장이 근거로 댄 기록의 내용이 아니면 버린다 — 그 기록은 보완 호출의 후보로 돌아간다."""
    wrong = {"text": "배포 체크리스트에 크론 확인 항목을 추가했습니다.", "refs": ["N5"]}   # N5 는 풀 사용률 경보 기록이다
    calls = _llm(monkeypatch, _main(wrong, NOTICE, RUNBOOK), {report.SYSTEM_PREVENTION: {"prevention": [ALARM]}})
    out, _, trace = await _fill()
    assert [s["text"] for s in out["prevention"]] == [NOTICE["text"], RUNBOOK["text"], ALARM["text"]]
    assert trace["main_prevention"]["dropped"] == [["기록에 없는 낱말", wrong["text"]]]
    assert calls[1]["user"] == "\n".join(["[기록]", ACTION_LINES[0]])   # 버려진 문장의 근거 기록만 다시 준다


def test_main_call_prevention_ending_as_a_recommendation_is_dropped():
    """본 호출의 재발 방지 문장도 권고 · 예정으로 끝나면 싣지 않는다(보완 호출과 같은 기준)."""
    data = {"summary": SUMMARY, "prevention": [{"text": "풀 사용률 점검을 권장했습니다.", "refs": ["N3"]}, {"text": "풀 크기 재검토가 필요합니다.", "refs": ["N3"]},
                                               {"text": "풀 상한 규칙을 검토할 필요가 있습니다.", "refs": ["N3"]}, {"text": "안내문 보강을 검토 중입니다.", "refs": ["N3"]},
                                               {"text": "풀을 20 으로 늘렸습니다.", "refs": ["N3"]}]}
    assert [s["text"] for s in report.merge(CONTENT, data)["prevention"]] == ["풀을 20 으로 늘렸습니다."]


@pytest.mark.parametrize("before,after", [
    ("배송 지연을 근거로 팀장 승인으로 전액 환불이 이루어졌습니다.", "배송 지연을 근거로 책임자 승인으로 전액 환불이 이루어졌습니다."),
    ("미입력 시 조직장에게 자동 알림을 설정했습니다.", "미입력 시 책임자에게 자동 알림을 설정했습니다."),
    ("인사팀장이 승인했습니다.", "책임자가 승인했습니다."),                       # 부서 이름째로, 조사는 받침에 맞춘다
    ("고객지원 팀장은 승인하지 않았고, 임원을 거쳤습니다.", "책임자는 승인하지 않았고, 책임자를 거쳤습니다."),
    ("확인 뒤, 팀장과 협의했습니다.", "확인 뒤, 책임자와 협의했습니다."),           # 앞의 띄어쓰기는 그대로
    ("과장 광고 문구를 확인했습니다.", "과장 광고 문구를 확인했습니다."),           # 다른 뜻이 있는 낱말은 건드리지 않는다
    ("법정대리인이 대리 수령했습니다.", "법정대리인이 대리 수령했습니다."),
    ("운영임원회에 보고했습니다.", "운영임원회에 보고했습니다."),                   # 다른 낱말의 일부
])
def test_external_sentence_replaces_job_titles_with_a_neutral_word(before, after):
    assert report.neutral(before) == after


@pytest.mark.parametrize("before,after", [
    ("장문경 매니저가 재기동했습니다.", "담당자가 재기동했습니다."),               # 이름 뒤의 직급째로
    ("배종현이 승인했습니다.", "담당자가 승인했습니다."),                         # 받침 있는 이름 뒤의 조사를 맞춘다
    ("홍경표는 로그를 확인했습니다.", "담당자는 로그를 확인했습니다."),           # 받침 없는 이름은 조사가 그대로 맞는다
    ("신동준 팀장님에게 보고했습니다.", "담당자에게 보고했습니다."),
    ("배종현 팀장과 장문경을 불렀습니다.", "담당자와 담당자를 불렀습니다."),
    ("홍경표준 문서를 고쳤습니다.", "홍경표준 문서를 고쳤습니다."),               # 이름으로 시작하는 다른 낱말은 건드리지 않는다
])
def test_external_sentence_replaces_staff_names(before, after):
    assert report.neutral(before, ["장문경", "배종현", "홍경표", "신동준"], ["매니저", "팀장"]) == after


def test_externalize_rewrites_every_ai_sentence_and_leaves_facts():
    data = {"summary": {"problem": {"text": "팀장 승인 없이 배포됐습니다.", "refs": ["N1"]}, "cause": {"text": "장문경이 풀을 줄였습니다.", "refs": ["N2"]},
                        "resolution": {"text": "임원 승인으로 풀을 늘렸습니다.", "refs": ["N3"]}},
            "impact": [{"text": "조직장에게 알림이 가지 않았습니다.", "refs": ["N1"]}],
            "causes": {"N2": {"text": "팀장이 확인하지 않았습니다.", "refs": ["N2"]}}, "side_issues": {"N4": {"text": "장문경 매니저가 한도를 올렸습니다.", "refs": ["N4"]}},
            "resolution_note": {"text": "본부장 승인 절차가 생겼습니다.", "refs": ["N3"]}, "prevention": [{"text": "팀장이 점검표를 만들었습니다.", "refs": ["N3"]}]}
    titled = {**CONTENT, "meta": {**CONTENT["meta"], "title": "팀장 승인 누락"}}
    out = report.externalize(report.merge(titled, data), ["장문경"], ["매니저"])
    assert [s["text"] for s in out["summary"]] == ["책임자 승인 없이 배포됐습니다.", "담당자가 풀을 줄였습니다.", "책임자 승인으로 풀을 늘렸습니다."]
    assert out["impact"][0]["text"] == "책임자에게 알림이 가지 않았습니다." and out["causes"][0]["explanation"]["text"] == "책임자가 확인하지 않았습니다."
    assert out["side_issues"][0]["note"]["text"] == "담당자가 한도를 올렸습니다." and out["resolution"]["note"]["text"] == "책임자 승인 절차가 생겼습니다."
    assert out["prevention"][0]["text"] == "책임자가 점검표를 만들었습니다." and out["prevention"][0]["refs"] == ["N3"]
    assert out["meta"]["title"] == "팀장 승인 누락"                               # 사실 칸은 건드리지 않는다
    empty = report.externalize(report.merge(CONTENT, {"summary": SUMMARY}), ["장문경"], [])
    assert empty["causes"][0]["explanation"] is None and empty["resolution"]["note"] is None   # 빈 칸은 빈 채로


async def test_fill_falls_back_to_plain_json_when_the_provider_rejects_a_schema(monkeypatch):
    """스키마 응답을 못 받는 공급자(400)면 스키마 없이 한 번 다시 보내 같은 결과를 받는다."""
    with OpenAIStub() as stub:
        stub.refuse_schema()
        monkeypatch.setenv("AI_MODE", "openai")
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_BASE_URL", stub.base_url)
        monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
        out = await report.fill(profile(), CONTENT, BODIES, False, llm.Usage())
        formats = [c["body"]["response_format"]["type"] for c in stub.calls if c["path"].endswith("/chat/completions")]
    assert formats[:2] == ["json_schema", "json_object"]
    assert out["summary"] == [{"text": "스텁 보고서 요약입니다.", "refs": ["N1"]}]


@requires_db
async def test_external_job_rewrites_names_and_titles_in_reused_sentences(monkeypatch, admin_conn):
    """외부용은 옮겨 쓴 문장에서도 사내 사람 이름과 직책 낱말을 바꿔 쓴다 — LLM 은 부르지 않는다."""
    from app.retrieval import db

    async def never(*a, **k):
        raise AssertionError("LLM must not be called")

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", never)
    name = admin_conn.execute("""SELECT u.name FROM users u JOIN workspaces w ON w.tenant_id = u.tenant_id
                                 WHERE w.id = (SELECT workspace_id FROM log_nodes ORDER BY id LIMIT 1) AND u.status = 'active' ORDER BY u.id LIMIT 1""").fetchone()["name"]
    done = report.merge(CONTENT, {"summary": {"problem": {"text": f"{name}에게 보고했고 팀장 승인으로 풀을 늘렸습니다.", "refs": ["N1"]}}})
    a, b = _insert(admin_conn, done), _insert(admin_conn, CONTENT, "external")
    admin_conn.execute("UPDATE incident_reports SET status = 'done', prompt_version = %s, model = 'stub-answer' WHERE id = %s", (report.PROMPT_VERSION, a))
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 1, "report_failed": 0}
        rows = {r["id"]: r["content"]["summary"][0]["text"] for r in admin_conn.execute("SELECT id, content FROM incident_reports WHERE id = ANY(%s)", ([a, b],)).fetchall()}
        assert rows[b] == "담당자에게 보고했고 책임자 승인으로 풀을 늘렸습니다."
        assert rows[a] == f"{name}에게 보고했고 팀장 승인으로 풀을 늘렸습니다."     # 내부용은 그대로
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([a, b],))


# ── 재발 방지 표시: 팀이 표시한 기록마다 한 문장 ─────────────────────────────

MARKED = {**ACTED, "prevention_refs": ["N6", "N5", "N4", "N99"]}   # N4 는 추가 문제 기록 · N99 는 없는 번호 — 표시로 치지 않는다


def _marked_llm(monkeypatch, answers: dict) -> list[dict]:
    """본 호출에는 재발 방지 문장이 든 응답을, 표시된 기록의 호출에는 answers[그 기록의 글에 든 낱말](목록이면 차례로)을 돌려주는 가짜 LLM."""
    calls: list[dict] = []

    async def chat(profile, model, system, user, usage, max_tokens=None, schema=None):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens, "schema": schema})
        usage.prompt_tokens += 10
        usage.completion_tokens += 2
        if system.startswith(report.SYSTEM):
            return _main(RUNBOOK)
        assert system.startswith(report.SYSTEM_MARKED), system[:40]   # 표시가 있으면 고르는 보완 호출은 부르지 않는다
        v = answers[next(k for k in answers if k in user)]
        answer = (v.pop(0) if len(v) > 1 else v[0]) if isinstance(v, list) else v
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setenv("AI_MODE", "openai")
    monkeypatch.setenv("LLM_PROFILE_DEFAULT_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_chat_json", chat)
    return calls


def test_marks_count_only_on_fix_and_action_records_in_record_order():
    assert report.marked_prevention(MARKED) == ["N5", "N6"] and report.marked_prevention(ACTED) == []
    assert report.report_schema(MARKED)["properties"]["prevention"] == {"type": "null"}          # 표시가 있으면 본 호출은 이 칸을 쓰지 않는다
    assert report.merge(MARKED, _main(RUNBOOK))["prevention"] == []                               # 본 호출이 써 와도 싣지 않는다


async def test_marked_records_get_one_sentence_each_from_their_own_record(monkeypatch):
    """표시된 기록 하나에 호출 하나 — 그 기록 한 줄만 주고 한 문장을 받는다. 근거는 그 기록 하나이고, AI 가 고른 문장은 싣지 않는다."""
    calls = _marked_llm(monkeypatch, {"경보": {"text": ALARM["text"]}, "공지": {"text": NOTICE["text"]}})
    out, usage, trace = await _fill(MARKED)
    assert out["prevention"] == [{**ALARM, "owner": None, "due": None}, {**NOTICE, "owner": None, "due": None}]
    small = sorted((c for c in calls[1:]), key=lambda c: c["user"])
    assert [c["user"] for c in small] == sorted(f"(조치) {line.split(' ', 2)[2]}" for line in ACTION_LINES[:2])
    assert all(c["system"] == report.SYSTEM_MARKED and c["max_tokens"] == report.MARKED_MAX_TOKENS for c in small) and len(calls) == 3
    assert calls[0]["schema"]["properties"]["prevention"] == {"type": "null"}
    assert trace["marked"]["marked"] == ["N5", "N6"] and trace["marked"]["fallback"] == [] and "prevention" not in trace
    assert (usage.prompt_tokens, usage.completion_tokens) == (30, 6)


async def test_marked_sentence_that_fails_a_condition_is_asked_once_more(monkeypatch):
    calls = _marked_llm(monkeypatch, {"경보": [{"text": "풀 사용률 80% 경보를 추가함."}, {"text": ALARM["text"]}], "공지": {"text": NOTICE["text"]}})
    out, _, trace = await _fill(MARKED)
    assert [s["text"] for s in out["prevention"]] == [ALARM["text"], NOTICE["text"]] and trace["marked"]["fallback"] == []
    again = [c for c in calls if c["user"].endswith(report.MARKED_AGAIN["내용"])]
    assert len(again) == 1 and "경보" in again[0]["user"] and len(calls) == 4


async def test_marked_record_falls_back_to_its_title_so_it_never_disappears(monkeypatch):
    """두 번 모두 조건에 못 미치거나(다른 내용을 씀) 호출이 실패하면 그 기록의 제목을 싣는다 — 표시한 기록이 표에서 빠지지 않는다."""
    _marked_llm(monkeypatch, {"경보": {"text": "배포 체크리스트를 새로 만들었습니다."}, "공지": RuntimeError("boom")})
    out, _, trace = await _fill(MARKED)
    assert [(s["text"], s["refs"]) for s in out["prevention"]] == [("풀 사용률 경보 추가", ["N5"]), ("고객 공지 발송", ["N6"])]
    assert sorted(trace["marked"]["fallback"]) == ["N5", "N6"] and "error" in trace["marked"]["raw"]["N6"][0]


def test_reuse_copies_marked_sentences_only_when_the_marks_are_the_same():
    done = {**report.merge(MARKED, _main()), "prevention": [{**ALARM, "owner": "김담당", "due": None}, {**NOTICE, "owner": None, "due": None}]}
    out = report.reuse(MARKED, done, ACTED_BODIES)
    assert out["prevention"] == [{**ALARM, "owner": None, "due": None}, {**NOTICE, "owner": None, "due": None}]
    # 표시가 달라졌으면 옮기지 않고 새로 만든다 — 늘었든 줄었든 모두 지웠든. 표시는 LLM 입력에 없어 입력 비교만으로는 걸리지 않는다
    assert report.reuse({**MARKED, "prevention_refs": ["N5", "N6", "N7"]}, done, ACTED_BODIES) is None
    assert report.reuse({**MARKED, "prevention_refs": ["N5"]}, done, ACTED_BODIES) is None
    assert report.reuse({**MARKED, "prevention_refs": []}, done, ACTED_BODIES) is None        # 표시를 지운 뒤 — 표시가 있던 때의 문장이 외부용에 실리지 않는다
    assert report.reuse(ACTED, done, ACTED_BODIES) is None
    picked = {**report.merge(ACTED, _main()), "prevention": [{**ALARM, "owner": None, "due": None}]}   # 표시 없이 AI 가 고른 내부용
    assert report.reuse(MARKED, picked, ACTED_BODIES) is None                                    # 그 뒤에 표시했으면 고른 문장을 표시한 것처럼 옮기지 않는다
    assert report.reuse(ACTED, picked, ACTED_BODIES)["prevention"] == picked["prevention"]     # 표시가 없던 그대로면 전처럼 옮긴다


def _prev(sentences: dict[int, str], shift: bool = True) -> dict:
    """앞 보고서 — 표시된 기록(기록 ID → 문장)이 그때의 글 그대로 실려 있다. shift 면 그때는 기록 번호가 하나씩 뒤였다(N5 → N6 …)."""
    recs = [{**r, "ref": f"N{int(r['ref'][1:]) + 1}"} for r in MARKED["records"]] if shift else MARKED["records"]
    ref = {r["id"]: r["ref"] for r in recs}
    texts = report.record_texts(MARKED, ACTED_BODIES)
    now = {r["id"]: r["ref"] for r in MARKED["records"]}
    return {"records": recs, "prevention": [{"text": t, "refs": [ref[i]], "owner": None, "due": None} for i, t in sentences.items()],
            "marked_src": {str(i): report.marked_src("조치", texts[now[i]]) for i in sentences}}


async def test_marked_sentence_of_an_unchanged_record_is_kept_from_the_previous_report(monkeypatch):
    """기록의 글이 앞 보고서 때와 같으면 호출하지 않고 그 문장을 그대로 쓴다 — 번호가 밀려도 기록 ID 로 찾는다. 글이 바뀐 기록만 새로 받는다."""
    calls = _marked_llm(monkeypatch, {"공지": {"text": NOTICE["text"]}})   # '경보' 기록의 호출이 나가면 답이 없어 제목으로 실린다 — 아래 단언이 깨진다
    usage, trace = llm.Usage(), {}
    prev = _prev({5: "풀 사용률이 80% 를 넘으면 울리는 경보를 추가했습니다."})
    out = await report.fill(profile(), MARKED, ACTED_BODIES, False, usage, trace, prev=prev)
    assert [(s["text"], s["refs"]) for s in out["prevention"]] == [("풀 사용률이 80% 를 넘으면 울리는 경보를 추가했습니다.", ["N5"]), (NOTICE["text"], ["N6"])]
    assert trace["marked"]["reused"] == ["N5"] and [c["user"] for c in calls[1:]] == [f"(조치) {ACTION_LINES[1].split(' ', 2)[2]}"]
    assert sorted(out["marked_src"]) == ["5", "6"]     # 둘 다 다음 보고서가 다시 쓸 수 있다

    changed = {**ACTED_BODIES, 5: "풀 사용률 70% 경보로 바꿨다"}   # 그 기록의 글이 바뀌면 새로 받는다
    calls = _marked_llm(monkeypatch, {"경보": {"text": "풀 사용률 70% 경보로 바꿨습니다."}, "공지": {"text": NOTICE["text"]}})
    trace = {}
    out = await report.fill(profile(), MARKED, changed, False, llm.Usage(), trace, prev=prev)
    assert out["prevention"][0]["text"] == "풀 사용률 70% 경보로 바꿨습니다." and trace["marked"]["reused"] == [] and len(calls) == 3


async def test_only_sentences_that_met_the_conditions_are_kept_for_the_next_report(monkeypatch):
    """제목으로 대신 실린 것 · 모양이 어긋난 채 실린 것은 지문을 남기지 않는다 — 다음 보고서에서 다시 받는다. 지문이 없는 앞 보고서(옛 것)에서는 아무것도 옮기지 않는다."""
    _marked_llm(monkeypatch, {"경보": [{"text": TWO}, {"text": PLUS}], "공지": RuntimeError("timeout")})
    out, _, trace = await _fill(MARKED)
    assert trace["marked"]["loose"] == ["N5"] and trace["marked"]["fallback"] == ["N6"] and out["marked_src"] == {}
    assert report.kept_marked(out) == {} and report.kept_marked(None) == {}
    old = {k: v for k, v in _prev({5: "예전 문장입니다."}).items() if k != "marked_src"}
    assert report.kept_marked(old) == {}


@requires_db
async def test_job_keeps_marked_sentences_only_when_the_records_changed(monkeypatch, admin_conn):
    """워커 경로 — 기록이 달라져 다시 만드는 보고서(sig 가 다름)는 앞 보고서의 문장을 옮겨 쓰고, 기록이 그대로인데 다시 만드는 보고서(sig 가 같음)는 모두 새로 받는다."""
    from app.retrieval import db
    alarm, notice = "풀 사용률 경보를 추가했습니다.", "고객 공지를 발송했습니다."   # 제목만으로 만든 줄 글과 낱말이 겹치는 문장
    calls = _marked_llm(monkeypatch, {"경보": {"text": alarm}, "공지": {"text": notice}})
    # 시험용 기록 ID 는 실제 기록이 아니다 — DB 의 본문과 상관없이 제목만으로 줄 글을 만든다
    monkeypatch.setattr(report, "record_texts", lambda content, bodies: {r["ref"]: r["title"] for r in content["records"]})
    kept = "풀 사용률이 80% 를 넘으면 울리는 경보를 추가했습니다."
    done = {**MARKED, "summary": [{"text": "풀이 고갈됐습니다.", "refs": ["N1"]}], "prevention": [{"text": kept, "refs": ["N5"], "owner": None, "due": None}],
            "marked_src": {"5": report.marked_src("조치", "풀 사용률 경보 추가")}}
    a, b = _insert(admin_conn, done), _insert(admin_conn, MARKED)
    model = profile().model_answer
    admin_conn.execute("UPDATE incident_reports SET status = 'done', prompt_version = %s, model = %s WHERE id = %s", (report.PROMPT_VERSION, model, a))
    c = None
    try:
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 1, "report_failed": 0}
        row = admin_conn.execute("SELECT sig, content FROM incident_reports WHERE id = %s", (b,)).fetchone()
        assert [s["text"] for s in row["content"]["prevention"]] == [kept, notice] and len(calls) == 2      # 본 호출 + '공지' 기록 하나
        assert sorted(row["content"]["marked_src"]) == ["5", "6"]
        calls.clear()
        c = _insert(admin_conn, MARKED)
        admin_conn.execute("UPDATE incident_reports SET sig = %s WHERE id = %s", (row["sig"], c))    # 기록이 그대로인데 다시 만들기
        with db.connect() as conn:
            assert await report.run_pending(conn) == {"reports": 1, "report_failed": 0}
        again = admin_conn.execute("SELECT content FROM incident_reports WHERE id = %s", (c,)).fetchone()["content"]
        assert [s["text"] for s in again["prevention"]] == [alarm, notice] and len(calls) == 3      # 본 호출 + 표시된 기록 둘
    finally:
        admin_conn.execute("DELETE FROM incident_reports WHERE id = ANY(%s)", ([x for x in (a, b, c) if x],))


TWO = "풀 사용률 경보를 추가했습니다. 풀 사용률 80% 경보를 추가했습니다."   # N5 의 글과 겹치지만 문장이 둘이다
PLUS = "풀 사용률 경보 + 80% 경보를 추가했습니다."
LONG = "풀 사용률 80% 경보를 추가했다는 것을 " + "풀 사용률 경보를 추가하고 " * 8 + "추가했습니다."


@pytest.mark.parametrize("first,why", [(TWO, "두 문장"), (PLUS, "기호"), (LONG, "길이")])
async def test_marked_sentence_with_a_wrong_shape_is_asked_again_with_the_reason(monkeypatch, first, why):
    """모양만 어긋난 문장(두 문장 · '+' 로 이은 메모 말투 · 기록을 통째로 옮긴 긴 문장)은 무엇이 어긋났는지 알려 주고 다시 받는다."""
    assert report.marked_problem({"text": first}, ACTION_LINES[0]) == why and report.marked_problem({"text": ALARM["text"]}, ACTION_LINES[0]) is None
    calls = _marked_llm(monkeypatch, {"경보": [{"text": first}, {"text": ALARM["text"]}], "공지": {"text": NOTICE["text"]}})
    out, _, trace = await _fill(MARKED)
    assert [s["text"] for s in out["prevention"]] == [ALARM["text"], NOTICE["text"]] and trace["marked"]["loose"] == []
    assert [c["user"].endswith(report.MARKED_AGAIN[why]) for c in calls if c["system"] == report.SYSTEM_MARKED and "경보" in c["user"]] == [False, True]


async def test_marked_sentence_whose_shape_stays_wrong_is_kept_rather_than_the_title(monkeypatch):
    """다시 받아도 모양만 어긋나면 그 문장을 싣는다(먼저 받은 것) — 제목보다 낫고 내용이 빠지지 않는다. 내용이 어긋난 답은 쓰지 않는다."""
    _marked_llm(monkeypatch, {"경보": [{"text": TWO}, {"text": PLUS}], "공지": [{"text": "고객에게 사과 공지 + 발송했습니다."}, {"text": "런북을 새로 만들었습니다."}]})
    out, _, trace = await _fill(MARKED)
    assert out["prevention"][0]["text"] == TWO and out["prevention"][1]["text"] == "고객에게 사과 공지 + 발송했습니다."
    assert sorted(trace["marked"]["loose"]) == ["N5", "N6"] and trace["marked"]["fallback"] == []
