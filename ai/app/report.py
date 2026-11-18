"""장애 보고서(R1)의 AI 칸 — incident_reports.status = generating 을 집어(SKIP LOCKED) LLM 1회로 채우고 done, 실패는 failed (워커 한 주기).

사실 칸(경과 · 원인 · 조치 · 규정 · 근거 기록)은 Spring 이 content 에 넣어 두었다. 여기서는 그 기록 번호(N1…)와 기록 본문을 LLM 에 주고
한눈에 보기 · 영향 · 원인 설명 · 조치 결과 · 추가 문제 메모 · 재발 방지 · 남은 확인만 받는다. 문장마다 근거 기록 번호가 있어야 하고(없으면 그 문장은 버린다),
content 에 없는 번호는 지운다. 키가 없으면(fake) AI 칸 없이 done — 화면이 고정 문구를 보인다. 실패해도 사실 칸은 그대로 남는다.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import ai_mode, profile as load_profile
from app.llm import client as llm
from app.retrieval import db

PROMPT_VERSION = "2"
MAX_TOKENS = 1500        # 출력 상한. 문장 15개 안팎이면 넉넉하다 — 여기서 잘리면 JSON 이 닫히지 않아 failed 가 된다
BODY_MAX = 500           # 기록 본문은 앞부분만
SENTENCE_MAX = 300       # 한 문장 상한(넘으면 자른다)
LIST_MAX = 5             # 한 칸의 문장 수 상한
STUCK_MINUTES = 10       # 이보다 오래 generating 이면 멈춘 요청 — Spring ReportController(ROWS · 재사용 쿼리)의 10분과 같은 값이어야 한다
# 해결 기록이 없는 장애 건의 한눈에 보기 마지막 문장(AI 가 쓴 문장 대신 넣는다) — 진행 중이면 앞의 것, 완료 · 참고면 뒤의 것(머리의 "완료"와 "아직"이 부딪히지 않게)
NO_FIX_OPEN, NO_FIX_DONE = "아직 해결 기록이 없습니다.", "해결 기록으로 지정된 기록이 없습니다."
KST = ZoneInfo("Asia/Seoul")
KIND_KO = {"occurrence": "발생", "cause": "원인", "resolution": "해결", "action": "조치", "side_issue": "추가 문제", "reference": "참고"}
STATUS_KO = {"resolved": "완료", "open": "진행 중", "reference": "참고"}
RELATION_KO = {"follows": "규정 따름", "exception": "규정 예외", "gap": "규정 없음"}

SYSTEM = """사내 장애 건 하나의 기록으로 장애 보고서의 문장 칸을 한국어로 채운다. 날짜 · 경과 표 · 원인과 조치 목록은 이미 만들어져 있고, 여기서는 문장만 쓴다.
규칙: (1) 아래 [기록]에 적힌 내용만 쓴다. 기록에 없는 원인 · 영향 · 수치 · 담당자 · 기한을 지어내거나 일반 상식으로 채우지 않는다. 쓸 내용이 기록에 없으면 그 칸은 빈 배열(또는 null)로 둔다.
(2) 문장마다 그 문장의 근거가 된 기록 번호를 refs 에 넣는다(예: ["N2", "N4"]). [기록]에 있는 번호만 쓴다. 근거 번호를 댈 수 없는 문장은 쓰지 않는다.
(2-1) 입력 첫머리의 "'원인' 기록 N건" · "'해결' 기록 N건" 줄이 summary 와 causes, resolution_note 에서 빠뜨리면 안 되는 기록이다 — 쓰고 나서 그 번호가 모두 들어갔는지 확인한다.
"'조치' 기록 N건" 줄은 prevention 을 쓸 때 살펴볼 기록이다 — 그 가운데 재발을 막으려고 한 일(점검 항목 · 경보 · 표준 · 안내를 더한 것 등)은 prevention 에 쓰고, '해결' 기록에 재발 방지가 함께 적혀 있으면 그것도 쓴다.
"'추가 문제' 기록 N건" 줄의 기록은 언제 나온 기록이든 모두 side_issues 에 한 문장씩 쓴다 — 원인으로 표시되지 않은 기록이므로 causes 에는 쓰지 않는다(causes 의 키는 '원인' 기록 번호뿐이다).
(3) 구분의 뜻: '발생'은 장애 건 자체, '원인'은 원인으로 표시된 기록, '해결'은 이 장애를 실제로 해결한 기록, '조치'는 그 밖에 한 일(정리 · 후속 조치 포함),
'추가 문제'는 진행 중에 새로 드러난 문제, '참고'는 참고 자료다 — 참고 기록은 조치가 아니므로 무엇을 했다고 쓰지 않는다.
(4) 칸: summary = 세 칸, 칸마다 한 문장 — problem: 무엇이 문제였는지. cause: '원인' 기록이 여럿이면 한 문장에 모두 적는다(하나도 빠뜨리지 않고, refs 에 그 번호를 전부 넣는다).
resolution: '해결' 기록이 여럿이면 한 문장에 모두 적는다(하나도 빠뜨리지 않고, refs 에 그 번호를 전부 넣는다). '원인' 기록이 없으면 cause 는 null.
'해결' 기록이 없으면 resolution 은 null — '조치' 기록에 해결했다고 적혀 있어도 해결로 쓰지 않는다.
impact = 기록 본문에 적힌 영향 범위 · 지속 시간 · 대상을 그대로 옮긴다 — 기록에 없는 결과('배포가 지연됐다', '업무에 차질', '사용자 경험에 영향')를 덧붙이지 않고, 날짜를 빼서 기간을 계산하지 않고, 기록에 없는 '불가능했다' · '할 수 없었다' 같은 정도 표현도 쓰지 않는다. causes = '원인' 기록마다 그 원인이 왜 문제를 일으켰는지 한 문장(키는 그 기록 번호, 하나도 빠뜨리지 않는다).
resolution_note = '해결' 기록의 조치로 무엇이 달라졌는지 한 문장('해결' 기록이 여럿이면 모두 반영하고 refs 에 그 번호를 전부, 없으면 null). side_issues = '추가 문제' 기록마다 무엇이 드러났고 어떻게 됐는지 한 문장(키는 그 기록 번호). 기록이 '원인 후보' · '조사 중' · '재현 중' · '검토 중'이라고 적었으면 확정된 것처럼 쓰지 않고 그 표현을 그대로 둔다.
prevention = 이미 한 재발 방지 · 후속 조치만(owner · due 는 기록에 담당자 · 기한이 적혀 있을 때만, 아니면 null). 아직 하지 않은 일('필요', '검토 중')은 쓰지 않는다. 근거는 '해결' · '조치' 기록이어야 하고, 그런 기록이 없으면 빈 배열로 둔다 — 칸을 채우려고 '참고' 기록이나 규정을 끌어오지 않는다.
(5) 배열의 한 항목에는 한 문장만 넣는다(두 문장이면 항목 두 개). 문장은 '~습니다' 체로 끝낸다. 수치 · 기간 · 에러 코드는 기록에 적힌 그대로, 날짜는 'MM/DD' 로 쓴다. 목록 기호 · 제목 · 기록 번호를 문장 안에 쓰지 않는다.
JSON {"summary": {"problem": {"text": "…", "refs": ["N1"]}, "cause": {"text", "refs"} 또는 null, "resolution": {"text", "refs"} 또는 null}, "impact": [{"text", "refs"}], "causes": {"N2": {"text", "refs"}}, "resolution_note": {"text", "refs"} 또는 null,
"side_issues": {"N6": {"text", "refs"}}, "prevention": [{"text", "refs", "owner": null, "due": null}]} 만 낸다."""
EXTERNAL_RULE = "\n(6) 외부 공개용이다 — 사람 이름 · 직책을 쓰지 않고 owner 는 null 로 둔다."


def _day(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d")


def render(content: dict, bodies: dict[int, str]) -> str:
    """LLM 입력: 장애 건 한 줄 + 원인 · 해결 기록 번호 + 기록(번호 · 구분 · 날짜 · 제목 — 본문 앞부분 ← 이어 붙인 이유) + 규정 관계."""
    meta = content["meta"]
    head = f"[장애 건] {meta['title']} · 상태: {STATUS_KO.get(meta['status'], meta['status'])} · 최초 기록 {_day(meta['first_reported_at'])}"
    if meta.get("resolved_at"):
        head += f" · 완료 {_day(meta['resolved_at'])}"
    reason = {c["ref"]: c.get("link_reason") for c in content.get("causes", [])}
    summary = {i["ref"]: i.get("summary") for i in content.get("resolution", {}).get("items", [])}
    lines = [head]
    for label, refs in (("원인", [c["ref"] for c in content.get("causes", [])]), ("해결", list(summary))):
        if refs:   # 빠뜨리면 안 되는 기록을 따로 한 줄로 — 기록이 길면 뒤쪽 원인 · 해결이 한눈에 보기에서 빠진다
            lines.append(f"'{label}' 기록 {len(refs)}건: {' '.join(refs)}")
    acted = [r["ref"] for r in content["records"] if r["kind"] == "action"]
    if acted:   # 재발 방지 칸에서 살펴볼 기록 — 짚어 주지 않으면 그 칸을 통째로 비우는 일이 잦았다
        lines.append(f"'조치' 기록 {len(acted)}건: {' '.join(acted)}")
    sides = [s["ref"] for s in content.get("side_issues", [])]
    if sides:   # 해결보다 먼저 나온 추가 문제는 메모를 건너뛰거나 원인 칸에 쓰는 일이 잦았다
        lines.append(f"'추가 문제' 기록 {len(sides)}건: {' '.join(sides)}")
    lines.append("[기록]")
    for r in content["records"]:
        body = " ".join((bodies.get(r["id"]) or "").split())
        body = body if len(body) <= BODY_MAX else body[:BODY_MAX] + "…"
        line = f"{r['ref']} ({KIND_KO.get(r['kind'], r['kind'])}, {_day(r['at'])}) {r['title']}"
        if body and body != r["title"]:
            line += f" — {body}"
        if summary.get(r["ref"]):
            line += f" / 결과: {summary[r['ref']]}"
        if reason.get(r["ref"]):
            line += f" ← 이어 붙인 이유: {reason[r['ref']]}"
        lines.append(line)
    if content["resolution"].get("resolve_note"):
        lines.append(f"완료 사유: {content['resolution']['resolve_note']}")
    if content.get("policy"):
        lines.append("[규정]")
        for p in content["policy"]:
            note = f" ({p['note']})" if p.get("note") else ""
            lines.append(f"{p['ref']} {RELATION_KO.get(p['relation'], p['relation'])}: {p.get('document') or '-'} {p.get('article') or ''}{note}".rstrip())
    return "\n".join(lines)


def merge(content: dict, data: dict, external: bool = False) -> dict:
    """LLM 결과를 content 의 AI 칸에 넣는다. 근거 번호가 없는 문장 · content 에 없는 번호 · 형식이 다른 값은 버린다."""
    known = {r["ref"] for r in content["records"]}
    done = {r["ref"] for r in content["records"] if r["kind"] in ("resolution", "action")}

    def sentence(x, own: str | None = None) -> dict | None:
        """own = 그 칸이 설명하는 기록 번호(원인 설명 · 추가 문제 메모) — LLM 이 refs 를 비워도 그 기록이 근거다."""
        if not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"].strip():
            return None
        refs = list(dict.fromkeys(r for r in (x.get("refs") or []) if isinstance(r, str) and r in known))
        if not refs and own:
            refs = [own]
        if not refs:
            return None
        return {"text": llm._clip(x["text"].strip(), SENTENCE_MAX), "refs": refs}

    def sentences(key: str) -> list[dict]:
        v = data.get(key)
        return [s for s in map(sentence, v if isinstance(v, list) else []) if s][:LIST_MAX]

    def text(v) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    def summary() -> list[dict]:
        """한눈에 보기 = 문제 · 원인 · 해결 세 칸을 문장 배열로. 원인 · 해결은 팀이 표시한 기록이 있을 때만 싣는다 — 지시문만으로는 지켜지지 않았다.
        problem 이 없거나 모양이 다르면(옛 배열 등) 빈 배열 — 호출 측이 빈 결과로 보고 실패로 남긴다."""
        v = data.get("summary")
        problem = sentence(v.get("problem")) if isinstance(v, dict) else None
        if problem is None:
            return []
        cause = sentence(v.get("cause")) if content["causes"] else None
        if has_fix:
            fix = sentence(v.get("resolution"))
        else:   # 해결 기록이 없으면 AI 문장을 버리고 고정 문장(근거는 발생 기록)
            fix = {"text": NO_FIX_OPEN if content["meta"]["status"] == "open" else NO_FIX_DONE,
                   "refs": [next(r["ref"] for r in content["records"] if r["kind"] == "occurrence")]}
        return [s for s in (problem, cause, fix) if s]

    has_fix = bool(content["resolution"]["items"])
    out = dict(content)
    out["summary"], out["impact"] = summary(), sentences("impact")   # open_items(남은 확인)는 사실 칸 — Spring 이 조립한 그대로 둔다
    prevention = []
    for x in (data.get("prevention") if isinstance(data.get("prevention"), list) else [])[:LIST_MAX]:
        s = sentence(x)
        if s and done.intersection(s["refs"]):   # 재발 방지는 실제로 한 일('해결' · '조치' 기록)이 근거일 때만
            prevention.append({**s, "owner": None if external else text(x.get("owner")), "due": text(x.get("due"))})
    out["prevention"] = prevention
    by_ref = lambda key: data.get(key) if isinstance(data.get(key), dict) else {}  # noqa: E731
    out["causes"] = [{**c, "explanation": sentence(by_ref("causes").get(c["ref"]), c["ref"])} for c in content["causes"]]
    out["side_issues"] = [{**s, "note": sentence(by_ref("side_issues").get(s["ref"]), s["ref"])} for s in content["side_issues"]]
    out["resolution"] = {**content["resolution"], "note": sentence(data.get("resolution_note")) if has_fix else None}
    return out


async def fill(profile, content: dict, bodies: dict[int, str], external: bool, usage: llm.Usage) -> dict | None:
    """AI 칸을 채운 content. openai 모드가 아니거나 키가 없으면 None(호출 측이 AI 칸 없이 끝낸다). LLM 1회(답변 모델)."""
    if ai_mode() != "openai" or not profile.has_key:
        return None
    data = await llm._chat_json(profile, profile.model_answer, SYSTEM + (EXTERNAL_RULE if external else ""), render(content, bodies), usage,
                                max_tokens=MAX_TOKENS)
    if not isinstance(data, dict):
        raise ValueError("AI_BAD_OUTPUT: not a JSON object")
    return merge(content, data, external)


def reuse(content: dict, src: dict, bodies: dict[int, str]) -> dict | None:
    """외부용 content 에 내부용(src)의 AI 칸을 옮긴다 — LLM 을 다시 부르지 않는다.
    LLM 입력이 다르거나(기록이 바뀜) 내부용 문장에 작성자 이름이 들어 있으면 None(호출 측이 LLM 으로 새로 만든다)."""
    if render(src, bodies) != render(content, bodies):
        return None
    data = {"summary": src.get("summary"), "impact": src.get("impact"),
            "prevention": [{**p, "owner": None} for p in src.get("prevention") or []],   # 담당자 이름은 옮기지 않는다
            "causes": {c["ref"]: c.get("explanation") for c in src.get("causes", [])},
            "side_issues": {s["ref"]: s.get("note") for s in src.get("side_issues", [])},
            "resolution_note": src.get("resolution", {}).get("note")}
    names = {r.get("author") for r in src.get("records", [])}   # 보고자는 루트 기록의 작성자라 여기에 들어 있다
    written = json.dumps(data, ensure_ascii=False)
    if any(n and n in written for n in names):
        return None
    out = merge(content, data, external=True)
    out["summary"] = src.get("summary") or []   # 저장된 한눈에 보기는 이미 걸러진 문장 배열 — 세 칸으로 다시 거르지 않고 그대로 옮긴다
    return out


async def fill_one(conn) -> str | None:
    """generating 1건. 처리하는 동안 행 잠금을 쥔 채 끝에 커밋한다(워커가 둘이어도 같은 보고서를 두 번 만들지 않게)."""
    row = conn.execute("""SELECT id, workspace_id, root_id, audience, content FROM incident_reports
                          WHERE status = 'generating' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
    if row is None:
        conn.rollback()
        return None
    from psycopg.types.json import Jsonb
    try:
        content = row["content"]
        prof = load_profile(db.workspace_settings(conn, row["workspace_id"])["settings"].get("llm_profile", "default"))
        ids = [r["id"] for r in content["records"]]
        bodies = {r["id"]: r["body"] for r in conn.execute("SELECT id, body FROM log_nodes WHERE id = ANY(%s)", (ids,)).fetchall()}
        usage = llm.Usage()
        src = conn.execute("""SELECT content, model FROM incident_reports WHERE workspace_id = %s AND root_id = %s AND audience = 'internal'
                              AND status = 'done' AND prompt_version = %s ORDER BY id DESC LIMIT 1""",
                           (row["workspace_id"], row["root_id"], PROMPT_VERSION)).fetchone() if row["audience"] == "external" else None
        filled = reuse(content, src["content"], bodies) if src else None
        model = src["model"] if filled else prof.model_answer   # 옮겨 쓴 보고서는 토큰 0 · 내부용을 만든 모델
        if filled is None:
            filled = await fill(prof, content, bodies, row["audience"] == "external", usage)
        if filled is not None and not filled["summary"]:   # 한눈에 보기는 늘 한 문장 이상 — 비었으면 빈 결과다. 실패로 남겨 이전 보고서를 지우지 않는다
            raise ValueError("AI_EMPTY_OUTPUT: no summary sentence with a known ref")
        if filled is None:
            conn.execute("UPDATE incident_reports SET status = 'done', error = NULL, generated_at = now() WHERE id = %s", (row["id"],))
        else:
            conn.execute("""UPDATE incident_reports SET status = 'done', content = %s, prompt_version = %s, model = %s, prompt_tokens = %s,
                            completion_tokens = %s, error = NULL, generated_at = now() WHERE id = %s""",
                         (Jsonb(filled), PROMPT_VERSION, model, usage.prompt_tokens, usage.completion_tokens, row["id"]))
        conn.execute("DELETE FROM incident_reports WHERE workspace_id = %s AND root_id = %s AND audience = %s AND id < %s",
                     (row["workspace_id"], row["root_id"], row["audience"], row["id"]))   # 새 보고서가 생겼으니 이전 것은 지운다(실패하면 남긴다)
        result = "done"
    except Exception as e:  # noqa: BLE001 — 형식 오류 · 시간 초과 · 예상 못 한 오류 모두 실패로 남겨 다음 주기에 같은 건을 되풀이하지 않는다
        conn.rollback()
        conn.execute("UPDATE incident_reports SET status = 'failed', error = %s, generated_at = now() WHERE id = %s",
                     (f"{type(e).__name__}: {e}"[:500], row["id"]))
        result = "failed"
    conn.commit()
    return result


async def run_pending(conn, limit: int = 10) -> dict:
    """대기 중인 보고서를 처리한다(한 주기 최대 limit 건).
    먼저 멈춘 요청(STUCK_MINUTES 넘게 generating)을 failed 로 바꾼다 — 화면에는 이미 실패로 보인 요청이라 뒤늦게 AI 를 부르지 않는다.
    다른 워커가 처리 중인 행은 잠겨 있어 건너뛴다."""
    conn.execute("""UPDATE incident_reports SET status = 'failed', error = %s, generated_at = now()
                    WHERE id IN (SELECT id FROM incident_reports WHERE status = 'generating' AND created_at <= now() - make_interval(mins => %s)
                                 FOR UPDATE SKIP LOCKED)""",
                 (f"AI_TIMEOUT: not picked up by the AI worker within {STUCK_MINUTES} minutes", STUCK_MINUTES))
    conn.commit()
    stats = {"reports": 0, "report_failed": 0}
    for _ in range(limit):
        r = await fill_one(conn)
        if r is None:
            break
        stats["reports" if r == "done" else "report_failed"] += 1
    return stats
