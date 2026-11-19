"""장애 보고서(R1)의 AI 칸 — incident_reports.status = generating 을 집어(SKIP LOCKED) LLM 으로 채우고 done, 실패는 failed (워커 한 주기).

사실 칸(경과 · 원인 · 조치 · 규정 · 근거 기록)은 Spring 이 content 에 넣어 두었다. 여기서는 그 기록 번호(N1…)와 기록 본문을 LLM 에 주고
한눈에 보기 · 영향 · 원인 설명 · 조치 결과 · 추가 문제 메모 · 재발 방지 · 남은 확인만 받는다. 문장마다 근거 기록 번호가 있어야 하고(없으면 그 문장은 버린다),
content 에 없는 번호는 지운다. 키가 없으면(fake) AI 칸 없이 done — 화면이 고정 문구를 보인다. 실패해도 사실 칸은 그대로 남는다.
본 호출이 빠뜨린 것만 작은 호출로 보완한다(해결 문장 · 재발 방지 칸). 작은 호출은 실패하거나 조건을 못 채우면 본 호출의 결과를 그대로 둔다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import ai_mode, profile as load_profile
from app.llm import client as llm
from app.retrieval import db

PROMPT_VERSION = "3"
MAX_TOKENS = 1500        # 출력 상한. 문장 15개 안팎이면 넉넉하다 — 여기서 잘리면 JSON 이 닫히지 않아 failed 가 된다
FIX_MAX_TOKENS = 300     # 해결 문장만 다시 받는 작은 호출의 출력 상한
PREVENTION_MAX_TOKENS = 400   # 재발 방지 칸 보완 호출의 출력 상한
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
"'조치' 기록 N건" 줄은 prevention 을 쓸 때 살펴볼 기록이다 — 그 가운데 재발을 막으려고 한 일(점검 항목 · 경보 · 표준 · 안내를 더한 것 등)과 장애가 정리된 뒤의 후속 조치(사후 검토 회의, 고객 · 사용자에 대한 공지 · 사과 · 보상)는 prevention 에 쓰고, 장애를 멈추거나 복구하려고 그 과정에서 한 일(중단 · 되돌림 · 임시 허용 · 값 조정)은 쓰지 않는다. '해결' 기록에 재발 방지가 함께 적혀 있으면 그 부분도 한 항목으로 쓴다.
"'추가 문제' 기록 N건" 줄의 기록은 언제 나온 기록이든 모두 side_issues 에 한 문장씩 쓴다 — 원인으로 표시되지 않은 기록이므로 causes 에는 쓰지 않는다(causes 의 키는 '원인' 기록 번호뿐이다).
(3) 구분의 뜻: '발생'은 장애 건 자체, '원인'은 원인으로 표시된 기록, '해결'은 이 장애를 실제로 해결한 기록, '조치'는 그 밖에 한 일(정리 · 후속 조치 포함),
'추가 문제'는 진행 중에 새로 드러난 문제, '참고'는 참고 자료다 — 참고 기록은 조치가 아니므로 무엇을 했다고 쓰지 않는다.
(4) 칸: summary = 세 칸, 칸마다 한 문장 — problem: 무엇이 문제였는지. cause: '원인' 기록이 여럿이면 한 문장에 모두 적는다(하나도 빠뜨리지 않고, refs 에 그 번호를 전부 넣는다).
resolution: '해결' 기록이 여럿이면 한 문장에 모두 적는다(하나도 빠뜨리지 않고, refs 에 그 번호를 전부 넣는다). '원인' 기록이 없으면 cause 는 null.
'해결' 기록이 없으면 resolution 은 null — '조치' 기록에 해결했다고 적혀 있어도 해결로 쓰지 않는다.
impact = 기록 본문에 적힌 영향 범위 · 지속 시간 · 대상을 그대로 옮긴다 — 기록에 없는 결과('배포가 지연됐다', '업무에 차질', '사용자 경험에 영향')를 덧붙이지 않고, 날짜를 빼서 기간을 계산하지 않고, 기록에 없는 '불가능했다' · '할 수 없었다' 같은 정도 표현도 쓰지 않는다. causes = '원인' 기록마다 그 원인이 왜 문제를 일으켰는지 한 문장(키는 그 기록 번호, 하나도 빠뜨리지 않는다).
resolution_note = '해결' 기록의 조치로 무엇이 달라졌는지 한 문장('해결' 기록이 여럿이면 모두 반영하고 refs 에 그 번호를 전부, 없으면 null). side_issues = '추가 문제' 기록마다 무엇이 드러났고 어떻게 됐는지 한 문장(키는 그 기록 번호). 기록이 '원인 후보' · '조사 중' · '재현 중' · '검토 중'이라고 적었으면 확정된 것처럼 쓰지 않고 그 표현을 그대로 둔다.
prevention = 이미 한 재발 방지 · 후속 조치만(owner · due 는 기록에 담당자 · 기한이 적혀 있을 때만, 아니면 null). 아직 하지 않은 일('필요', '검토 중')은 쓰지 않는다. 근거는 '해결' · '조치' 기록이어야 하고, 그런 기록이 없으면 빈 배열로 둔다 — 칸을 채우려고 '참고' 기록이나 규정을 끌어오지 않는다.
(5) 배열의 한 항목에는 한 문장만 넣는다(두 문장이면 항목 두 개). 모든 칸의 문장은 완결된 문장으로 풀어 '~습니다 · ~입니다 · ~합니다'로 끝낸다 — 기록의 제목이나 본문이 '~음 · ~됨'이나 명사로 끝나 있어도 그대로 옮기지 않는다. 수치 · 기간 · 에러 코드는 기록에 적힌 그대로, 날짜는 'MM/DD' 로 쓴다. 목록 기호 · 제목 · 기록 번호를 문장 안에 쓰지 않는다.
JSON {"summary": {"problem": {"text": "…", "refs": ["N1"]}, "cause": {"text", "refs"} 또는 null, "resolution": {"text", "refs"} 또는 null}, "impact": [{"text", "refs"}], "causes": {"N2": {"text", "refs"}}, "resolution_note": {"text", "refs"} 또는 null,
"side_issues": {"N6": {"text", "refs"}}, "prevention": [{"text", "refs", "owner": null, "due": null}]} 만 낸다."""
EXTERNAL_RULE = "\n(6) 외부 공개용이다 — 사람 이름 · 직책을 쓰지 않고 owner 는 null 로 둔다."
# 해결 문장만 다시 받는 지시문. 규칙 (3)을 빼면 기록의 이유 · 수치를 다 옮겨 문장이 세 배로 길어진다
SYSTEM_FIX = """사내 장애 건의 '해결' 기록 여러 개를 한 문장으로 옮긴다.
규칙: (1) 아래 기록에 적힌 내용만 쓴다. (2) 기록을 하나도 빠뜨리지 않는다 — 기록마다 무엇을 했는지를 한 문장 안에 모두 적고, refs 에 모든 기록 번호를 넣는다.
(3) 기록마다 '무엇을 했는지'만 한 구절로 짧게 쓴다 — 이유 · 경과 · 결과 수치는 쓰지 않는다. 문장 전체는 120자를 넘기지 않는다.
(4) 완결된 문장으로 풀어 '~습니다'로 끝낸다. 기록 번호 · 목록 기호를 문장 안에 쓰지 않는다.
JSON {"text": "…", "refs": ["N2", "N4"]} 만 낸다."""
# 재발 방지 칸 보완 호출의 지시문. 구체적인 예시 낱말('명문화' · '검증' 등)을 넣지 않는다 — 넣으면 기록에 없는 그 낱말이 문장으로 나온다
SYSTEM_PREVENTION = """사내 장애 건의 기록에서 장애 보고서의 '재발 방지 · 후속 조치' 칸에 더 실을 문장을 고른다. 이 장애를 어떻게 멈추고 복구했는지는 보고서의 다른 칸에 이미 적혀 있다.
규칙: (1) 아래 [기록]에 적힌 사실만 옮긴다. 기록에 없는 조치 · 평가 · 효과를 덧붙이지 않는다. 문장의 낱말은 그 기록에 나온 낱말을 쓴다.
(2) 싣는 것: 같은 문제가 다시 생기지 않게 새로 만들거나 바꿔 둔 것, 그리고 장애가 정리된 뒤에 한 후속 조치(사후 검토, 고객 · 사용자에게 한 일).
(3) 싣지 않는 것: 장애를 멈추거나 복구하려고 한 일(중단 · 되돌림 · 재기동 · 임시 허용 · 수동 처리 · 취소 · 값이나 용량 조정), 아직 하지 않은 일, 앞으로 이렇게 하라는 권고.
(4) 실을 것이 없는 장애 건이 많다. 없으면 반드시 빈 배열을 낸다.
(5) 한 항목에 한 문장, 기록 하나에 한 항목. refs 에는 그 문장을 옮겨 온 기록 번호 하나를 넣는다. 완결된 문장으로 풀어 '~습니다'로 끝낸다. 기록 번호 · 목록 기호를 문장 안에 쓰지 않는다.
JSON {"prevention": [{"text": "…", "refs": ["N5"]}]} 만 낸다."""
# 보완 호출에 넘길 '해결' 기록을 고르는 낱말(조치 기록은 낱말과 상관없이 넘긴다). '이후' · '절차' · '규정' · '검수' · '자동' · '상한'은 흔한 말이라 복구 · 값 조정 기록이 걸린다 — 넣지 않는다
SIGNALS = ("자동화", "점검", "체크리스트", "검수표", "경보", "알림", "검증", "런북", "표준", "필수", "방지", "멱등", "분리", "회수", "임계", "명문화", "앞으로")
# 보완 호출이 준 문장의 검증 — 이 낱말이 문장에 있으면 근거 기록에도 있어야 하고(CLAIMS), 권고 · 예정 · 해결 되풀이로 끝나면 안 된다(NORM)
CLAIMS = ("명문화", "강화", "검증", "경보", "자동화", "점검", "체크리스트", "표준", "런북", "규정", "장치", "마련", "절차")
NORM = re.compile(r"(해야 합니다|권장합니다|필요합니다|예정입니다|예정되어 있습니다|계획입니다|문제를 해결했습니다|문제가 해결되었습니다)\.?$")
TOK = re.compile(r"[가-힣A-Za-z0-9]{2,}")
ENDINGS = ("했습니다", "하였습니다", "합니다", "있습니다", "되었습니다", "됩니다")
OVERLAP_MIN = 0.6        # 문장의 낱말이 근거 기록에 나오는 비율의 하한 — 다른 기록의 내용에 번호만 붙인 문장을 거른다


def _day(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d")


def record_texts(content: dict, bodies: dict[int, str]) -> dict[str, str]:
    """기록 번호 → 줄 글(제목 — 본문 앞부분 / 결과: 요약 ← 이어 붙인 이유). 본 호출과 작은 호출이 같은 글을 쓴다."""
    reason = {c["ref"]: c.get("link_reason") for c in content.get("causes", [])}
    summary = {i["ref"]: i.get("summary") for i in content.get("resolution", {}).get("items", [])}
    out = {}
    for r in content["records"]:
        body = " ".join((bodies.get(r["id"]) or "").split())
        body = body if len(body) <= BODY_MAX else body[:BODY_MAX] + "…"
        line = r["title"]
        if body and body != r["title"]:
            line += f" — {body}"
        if summary.get(r["ref"]):
            line += f" / 결과: {summary[r['ref']]}"
        if reason.get(r["ref"]):
            line += f" ← 이어 붙인 이유: {reason[r['ref']]}"
        out[r["ref"]] = line
    return out


def render(content: dict, bodies: dict[int, str]) -> str:
    """LLM 입력: 장애 건 한 줄 + 원인 · 해결 기록 번호 + 기록(번호 · 구분 · 날짜 · 줄 글) + 규정 관계."""
    meta = content["meta"]
    head = f"[장애 건] {meta['title']} · 상태: {STATUS_KO.get(meta['status'], meta['status'])} · 최초 기록 {_day(meta['first_reported_at'])}"
    if meta.get("resolved_at"):
        head += f" · 완료 {_day(meta['resolved_at'])}"
    texts = record_texts(content, bodies)
    lines = [head]
    for label, refs in (("원인", [c["ref"] for c in content.get("causes", [])]), ("해결", [i["ref"] for i in content.get("resolution", {}).get("items", [])])):
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
        lines.append(f"{r['ref']} ({KIND_KO.get(r['kind'], r['kind'])}, {_day(r['at'])}) {texts[r['ref']]}")
    if content["resolution"].get("resolve_note"):
        lines.append(f"완료 사유: {content['resolution']['resolve_note']}")
    if content.get("policy"):
        lines.append("[규정]")
        for p in content["policy"]:
            note = f" ({p['note']})" if p.get("note") else ""
            lines.append(f"{p['ref']} {RELATION_KO.get(p['relation'], p['relation'])}: {p.get('document') or '-'} {p.get('article') or ''}{note}".rstrip())
    return "\n".join(lines)


def _sentence(x, known: set[str], own: str | None = None) -> dict | None:
    """문장 하나의 검증 — 글이 있고 근거 번호가 known 에 있어야 한다.
    own = 그 칸이 설명하는 기록 번호(원인 설명 · 추가 문제 메모) — LLM 이 refs 를 비워도 그 기록이 근거다."""
    if not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"].strip():
        return None
    refs = list(dict.fromkeys(r for r in (x.get("refs") or []) if isinstance(r, str) and r in known))
    if not refs and own:
        refs = [own]
    if not refs:
        return None
    return {"text": llm._clip(x["text"].strip(), SENTENCE_MAX), "refs": refs}


def merge(content: dict, data: dict, external: bool = False) -> dict:
    """LLM 결과를 content 의 AI 칸에 넣는다. 근거 번호가 없는 문장 · content 에 없는 번호 · 형식이 다른 값은 버린다."""
    known = {r["ref"] for r in content["records"]}
    done = {r["ref"] for r in content["records"] if r["kind"] in ("resolution", "action")}

    def sentence(x, own: str | None = None) -> dict | None:
        return _sentence(x, known, own)

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
    note = sentence(data.get("resolution_note")) if has_fix else None
    if note and not {i["ref"] for i in content["resolution"]["items"]}.intersection(note["refs"]):
        note = None   # 해결 요약은 해결 기록이 근거여야 한다 — 일부만 댄 것은 둔다(바로 위 표에 기록마다 결과가 나온다)
    out["resolution"] = {**content["resolution"], "note": note}
    return out


def accept_fix(raw, fixes: list[str]) -> dict | None:
    """다시 받은 해결 문장의 조건 — 근거가 해결 기록 전부이고 그 밖의 번호가 없으며, '니다'로 끝나고 길이 상한 안이다. 아니면 None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not isinstance(raw.get("refs"), list):
        return None
    text, refs = raw["text"].strip(), [r for r in raw["refs"] if isinstance(r, str)]
    if len(refs) != len(raw["refs"]) or set(refs) != set(fixes) or not text.rstrip(".").endswith("니다") or len(text) > SENTENCE_MAX:
        return None
    return {"text": text, "refs": list(fixes)}


async def refetch_fix(profile, out: dict, has_sentence: bool, texts: dict[str, str], external: bool, usage: llm.Usage,
                      trace: dict | None = None) -> dict:
    """해결 기록이 둘 이상인데 한눈에 보기의 해결 문장이 그 번호를 다 담지 못했으면(문장이 없는 것 포함) 해결 기록만 주고 그 문장만 다시 받는다.
    조건을 못 채우거나 호출이 실패하면 원래 그대로다. 빠진 번호를 코드가 채워 넣지는 않는다 — 글에 없는 내용의 근거가 생긴다."""
    marked = {i["ref"] for i in out["resolution"]["items"]}
    fixes = [r["ref"] for r in out["records"] if r["ref"] in marked]
    if len(fixes) < 2 or not out["summary"]:   # 한눈에 보기가 비었으면 빈 결과다 — 작은 호출로 채워 실패를 가리지 않는다
        return out
    current = out["summary"][-1] if has_sentence else None
    if current and marked <= set(current["refs"]):
        return out
    note = {"before": current, "accepted": False}
    if trace is not None:
        trace["fix"] = note
    try:
        raw = await llm._chat_json(profile, profile.model_answer, SYSTEM_FIX + (EXTERNAL_RULE if external else ""),
                                   "\n".join(f"{k} {texts[k]}" for k in fixes), usage, max_tokens=FIX_MAX_TOKENS)
    except Exception as e:  # noqa: BLE001 — 작은 호출의 실패는 보고서의 실패가 아니다
        note["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    note["raw"] = raw
    again = accept_fix(raw, fixes)
    if again is None:
        return out
    note["accepted"] = True
    return {**out, "summary": [*(out["summary"][:-1] if current else out["summary"]), again]}


def overlap(text: str, src: str) -> float:
    """문장의 낱말(2자 이상) 가운데 앞 두 글자가 근거 기록의 글에 나오는 비율."""
    toks = [t for t in TOK.findall(text) if t not in ENDINGS]
    return sum(1 for t in toks if t[:2] in src) / max(1, len(toks))


def guard(text: str, src: str) -> str | None:
    """실으면 안 되는 이유. 없으면 None. src = 그 문장이 근거로 댄 기록의 줄 글."""
    if NORM.search(text.strip()):
        return "권고 · 예정 · 해결 되풀이로 끝남"
    if [w for w in CLAIMS if w in text and w not in src]:
        return "기록에 없는 낱말"
    if overlap(text, src) < OVERLAP_MIN:
        return "근거 기록과 겹침 60% 미만"
    return None


def checked_prevention(raw, texts: dict[str, str], given: list[str]) -> tuple[list[dict], list[list[str]]]:
    """보완 호출의 응답에서 (실을 문장, 버린 [이유, 문장]). 근거가 넘긴 기록이고, 기록 하나에 한 문장이며, '니다'로 끝나고, guard 를 통과해야 싣는다."""
    kept, dropped, used = [], [], set()
    for x in (raw.get("prevention") if isinstance(raw, dict) and isinstance(raw.get("prevention"), list) else [])[:LIST_MAX]:
        if not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"].strip():
            continue
        text = x["text"].strip()
        refs = list(dict.fromkeys(r for r in (x.get("refs") if isinstance(x.get("refs"), list) else []) if isinstance(r, str) and r in given))
        why = ("입력에 없는 근거 번호" if not refs else "같은 기록의 둘째 문장" if used.intersection(refs)
               else "'니다'로 끝나지 않음" if not text.rstrip(".").endswith("니다")   # 기록의 메모 말투를 그대로 옮긴 문장
               else guard(text, " ".join(texts[r] for r in refs)))
        if why:
            dropped.append([why, text])
        else:
            kept.append({"text": llm._clip(text, SENTENCE_MAX), "refs": refs})
            used.update(refs)
    return kept, dropped


async def more_prevention(profile, out: dict, texts: dict[str, str], external: bool, usage: llm.Usage, trace: dict | None = None) -> dict:
    """재발 방지 칸의 보완 — 본 호출이 이 칸의 근거로 쓰지 않은 '조치' 기록과 낱말(SIGNALS)이 든 '해결' 기록만 주고 더 실을 문장을 받아 뒤에 붙인다.
    해결 기록이 없는 장애 건에서는 부르지 않는다 — 그때의 조치 기록은 대응 그 자체다. 다른 칸의 문장은 입력에 넣지 않는다(베끼고 엉뚱한 번호를 붙인다)."""
    room = LIST_MAX - len(out["prevention"])
    cited = {r for s in out["prevention"] for r in s["refs"]}
    given = [r["ref"] for r in out["records"] if r["ref"] not in cited
             and (r["kind"] == "action" or (r["kind"] == "resolution" and any(w in texts[r["ref"]] for w in SIGNALS)))]
    if not out["resolution"]["items"] or not out["summary"] or not given or room <= 0:
        return out
    note = {"given": given, "added": [], "dropped": []}
    if trace is not None:
        trace["prevention"] = note
    kind = {r["ref"]: KIND_KO[r["kind"]] for r in out["records"]}
    try:
        raw = await llm._chat_json(profile, profile.model_answer, SYSTEM_PREVENTION + (EXTERNAL_RULE if external else ""),
                                   "[기록]\n" + "\n".join(f"{k} ({kind[k]}) {texts[k]}" for k in given), usage, max_tokens=PREVENTION_MAX_TOKENS)
    except Exception as e:  # noqa: BLE001 — 작은 호출의 실패는 보고서의 실패가 아니다
        note["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    note["raw"] = raw
    kept, note["dropped"] = checked_prevention(raw, texts, given)
    note["added"] = [{**s, "owner": None, "due": None} for s in kept[:room]]   # 담당자 · 기한은 본 호출만 쓴다
    return {**out, "prevention": [*out["prevention"], *note["added"]]}


async def fill(profile, content: dict, bodies: dict[int, str], external: bool, usage: llm.Usage, trace: dict | None = None) -> dict | None:
    """AI 칸을 채운 content. openai 모드가 아니거나 키가 없으면 None(호출 측이 AI 칸 없이 끝낸다). 본 호출 1회(답변 모델) + 빠뜨린 것이 있을 때만 작은 호출.
    trace 를 주면 작은 호출이 무엇을 받고 무엇을 버렸는지 적는다(점검 스크립트용 — 저장하는 content 에는 넣지 않는다)."""
    if ai_mode() != "openai" or not profile.has_key:
        return None
    data = await llm._chat_json(profile, profile.model_answer, SYSTEM + (EXTERNAL_RULE if external else ""), render(content, bodies), usage,
                                max_tokens=MAX_TOKENS)
    if not isinstance(data, dict):
        raise ValueError("AI_BAD_OUTPUT: not a JSON object")
    out = merge(content, data, external)
    v = data.get("summary")
    has_sentence = isinstance(v, dict) and _sentence(v.get("resolution"), {r["ref"] for r in content["records"]}) is not None
    texts = record_texts(content, bodies)
    out = await refetch_fix(profile, out, has_sentence, texts, external, usage, trace)
    return await more_prevention(profile, out, texts, external, usage, trace)


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
