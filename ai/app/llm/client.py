"""LLM 호출 계층. OpenAI 호환 chat/completions 를 httpx 로 호출(JSON 모드·스트리밍)하고, fake 모드는 규칙 기반으로 같은 인터페이스를 채운다.
그래프 노드는 이 모듈의 함수만 부르므로 공급자·모드 분기가 노드 안에 흩어지지 않는다.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

import httpx

from app.config import LlmProfile, ai_mode, answer_max_tokens
from app.llm.schemas import IntentResult, RefineResult
from app.retrieval.tokenize import Tokenizer, extract_error_code

# 규칙 분류 힌트. 한쪽 힌트만 있으면 LLM 없이 확정, 둘 다 있으면 both, 둘 다 없으면 LLM(openai) 또는 both(fake).
# 넓은 말("되나요", "거부")은 넣지 않는다 — 규정 FAQ 질문을 troubleshoot 로 잘못 보내면 규정 검색을 건너뛰기 때문.
POLICY_HINTS = ("규정", "기준", "지침", "정책", "신청", "절차", "환불", "환급", "교환", "승인", "조항", "몇 조", "규칙", "허용",
                "보증기간", "보유기간", "내용연수", "배상", "보상", "위약금", "수수료", "취소", "해지", "약관", "고시", "법령", "법에", "기한", "시한", "며칠", "언제까지", "청구",
                "보관", "파기", "유효기간", "주기", "금지", "서약", "동의")
TROUBLE_HINTS = ("오류", "에러", "장애", "해결", "예전에", "사례", "재발", "먹통", "타임아웃", "멈춘", "멈춤", "느려", "거절", "문제", "배치",
                 "timeout", "error", "exception", "stacktrace")
# 약한 힌트: 규정 질문에도 흔히 나온다("인증이 실패하나요"). 단독으로는 troubleshoot 확정 대신 LLM 에 넘긴다
# "복구" 도 약한 힌트다 — 장애 복구뿐 아니라 계정 복구(고객 FAQ)에도 쓰인다
TROUBLE_WEAK_HINTS = ("실패", "원인", "조치", "복구", "거부", "안 돼", "안돼", "안 됨", "안됨")
# 약한 규정 힌트: "~해야 해?"(의무·판단) · "몇 초/몇 회/상한"(기준값) 은 규정이 답을 가진 질문이다. 장애 힌트와 함께 오면 both,
# 단독이면 LLM 에 넘긴다 — 사례만 보고 "타임아웃은 20초" 처럼 로그 수치를 규정인 양 답하는 것을 막는다(실험 2026-10-29)
POLICY_WEAK_HINTS = ("해야 해", "해야 하", "해야 되", "해야 돼", "해야돼", "해야해", "해도 되", "해도 돼", "되나요", "몇 초", "몇 번", "몇 회", "몇 분",
                     "몇 시간", "몇 퍼센트", "몇 %", "상한", "한도", "최대", "최소", "기본값", "의무")
# 의무·허용·기준값을 묻는 어미와 수량 표현: "써야 하는", "끝내야 돼", "내보내도 돼", "몇 주", "얼마나" — 목록으로는 동사마다 다 적을 수 없다.
# 규정+사례형 부서(개발·총무)의 규정 질문은 "장애"·"배치" 같은 단어를 품고 있어, 이 표현이 없으면 사례 전용으로 분류돼 규정 검색이 빠진다
OBLIGATION_RE = re.compile(r"[가-힣]야\s?(?:해|하|돼|되|합|됩)|[가-힣]도\s?(?:돼|되|됩)|몇\s?(?:초|번|회|분|시간|시|일|주|개월|달|년|개|명|퍼센트|%|급|자)|얼마나|얼마까지|얼마 안")
# 경험을 묻는 표현: "어떻게 했어", "롤백한 적 있어", "만든 거", "얼마로 잡았어?" — 사례(DAG)가 답을 가진 질문이다. 규정 힌트와 함께 오면 both
EXPERIENCE_RE = re.compile(r"어떻게\s?(?:했|풀었|고쳤|해결했|처리했|막았)|어케|[가-힣]\s적\s?(?:있|없)|(?:만든|바꾼|고친|적용한|도입한)\s?(?:거|것|사례)"
                           r"|(?:했|잡았|풀었|고쳤|바꿨|막았|넣었|뺐|정했)(?:어|지|나|는지)(?:요)?\s*\??$")
# 증상을 호소하는 표현: "자꾸 로그아웃돼요", "이미지를 못 받아와요", "상신이 안 돼요" — 조치를 정한 규정과 사례를 함께 본다(both)
SYMPTOM_RE = re.compile(r"(?:자꾸|계속|갑자기|반복해서)\s?[^?]{0,20}(?:돼요|되요|돼|됨|겨요|져요|떠요|뜸|나요|남)|못\s?[가-힣]{1,6}(?:와요|해요|가요|옴|함)|안\s?(?:돼요|됩니다|되네)")
HTTP_STATUS_RE = re.compile(r"(?<!\d)[45]\d\d(?!\d)")   # '504', '429로', '403 떨어지는데' — 접두어 없는 HTTP 상태 코드


def rule_classify(query: str, error_code: str | None) -> IntentResult | None:
    """힌트만으로 결정되면 IntentResult, 애매하면 None. 에러 코드는 troubleshoot 의 강한 신호."""
    q = query.lower()
    has_policy = any(h in q for h in POLICY_HINTS)
    has_trouble = bool(error_code) or any(h in q for h in TROUBLE_HINTS) or bool(HTTP_STATUS_RE.search(q)) or bool(EXPERIENCE_RE.search(q))
    symptom = bool(SYMPTOM_RE.search(q))
    weak_trouble = any(h in q for h in TROUBLE_WEAK_HINTS)
    weak_policy = any(h in q for h in POLICY_WEAK_HINTS) or bool(OBLIGATION_RE.search(q))
    if has_policy and (has_trouble or weak_trouble):
        return IntentResult(intent="both", reason="rule: policy+trouble hints")
    if has_trouble and weak_policy:
        return IntentResult(intent="both", reason="rule: trouble + obligation/threshold hints")
    if has_policy:
        return IntentResult(intent="policy", reason="rule: policy hints")
    if weak_policy and not has_trouble and not weak_trouble:   # 장애 낌새 없이 의무·허용·기준값만 묻는다 → 규정이 답을 가진 질문
        return IntentResult(intent="policy", reason="rule: obligation/threshold hints")
    if has_trouble:
        return IntentResult(intent="troubleshoot", reason="rule: error code" if error_code else "rule: trouble hints")
    if symptom:
        return IntentResult(intent="both", reason="rule: symptom")
    return None


class Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.finish_reason: str | None = None   # 생성 스트림의 종료 사유 ("length" = max_tokens 에 걸려 잘림)


# ── 분류 ─────────────────────────────────────────────────────────────────────

async def classify(profile: LlmProfile, query: str, error_code: str | None, policy_enabled: bool, usage: Usage,
                   has_cases: bool = True, code_in_policy: bool = False) -> IntentResult:
    """규칙 먼저, LLM 은 애매할 때만 — 분류 LLM 호출(≈4 s)이 첫 토큰 체감의 대부분이라 힌트로 결정되면 건너뛴다.
    has_cases·code_in_policy 는 그 부서의 데이터에서 온다(db.classify_context) — 단어 목록이 아니라 회사마다 다른 사정으로 판단한다."""
    if not policy_enabled:
        return IntentResult(intent="troubleshoot", reason="dag_only mode")
    if not has_cases:   # 등록된 사례가 없는 부서(규정 참조형)는 사례 검색이 빈손이다 — 분류할 것 없이 규정만 본다
        return IntentResult(intent="policy", reason="rule: no cases in workspace")
    ruled = rule_classify(query, error_code)
    if ruled is not None:
        if ruled.intent == "troubleshoot" and error_code and code_in_policy:   # 그 에러 코드의 조치를 규정이 정해 두었다 → 규정도 함께
            return IntentResult(intent="both", reason="rule: error code appears in policy")
        return ruled
    if ai_mode() == "openai" and profile.has_key:
        system = ("You classify a Korean enterprise question into exactly one intent. "
                  "policy: asks about official rules/guidelines/procedures, or what one must/should do, or a threshold/limit/default value. "
                  "troubleshoot: asks how a failure/error was solved before. both: asks for both, or asks what to do about a failure. "
                  "If unsure, answer both. Reply JSON {\"intent\": \"policy|troubleshoot|both\", \"reason\": \"...\"}.")
        hint = f" (error code detected: {error_code})" if error_code else ""
        data = await _chat_json(profile, profile.model_light, system, f"질문: {query}{hint}", usage)
        try:
            return IntentResult(intent=data["intent"], reason="llm: " + str(data.get("reason", "")))
        except Exception:
            return IntentResult(intent="both", reason="llm: fallback")
    return IntentResult(intent="both", reason="rule: unsure → both")


JUDGE_SYSTEM = ("사용자 질문과 사내 해결 사례가 같은 문제를 다루는지 판단한다. "
                "같은 대상(시스템·물건·절차)의 같은 종류 문제면 relevant=true. 주제가 비슷해 보여도 대상이 다르면 false "
                "(예: 법인카드 분실 질문 ↔ 출입카드 인식 오류 사례 = false). 질문이 사례의 원인·해결·후속 조치를 묻는 것도 true. "
                'JSON 으로만 답한다: {"relevant": true|false}')


async def judge_case(profile: LlmProfile, query: str, case: dict, usage: Usage) -> bool:
    """사례 후보 하나가 질문과 같은 문제를 다루는가. 키가 없으면 판정하지 않고 True."""
    if ai_mode() != "openai" or not profile.has_key:
        return True
    user = f"질문: {query}\n\n사례 묶음 제목: {case['root_title']}\n해당 기록: {case['title']}\n요약: {(case.get('summary') or '')[:200]}"
    data = await _chat_json(profile, profile.model_light, JUDGE_SYSTEM, user, usage)
    if not isinstance(data.get("relevant"), bool):
        raise ValueError("judge: no 'relevant' boolean")
    return data["relevant"]


# ── 생성 (스트리밍) ──────────────────────────────────────────────────────────

GENERATE_SYSTEM = """당신은 기업 업무지식 어시스턴트다. 아래 컨텍스트만 근거로 한국어로 간결하게 답한다.
규칙: (1) 인용은 제공된 마커([P1], [N2] 등)만 쓴다. (2) 사실을 담은 모든 문장과 표의 모든 행 끝에 근거 마커를 붙인다. 컨텍스트에 없는 내용은 "근거 없음"이라고 말하고 일반 상식으로 채우지 않는다.
(3) [폐기됨] 표시 노드는 권장하지 않는다. (4) 규정 근거와 해결 사례를 구분해 쓴다. 단, 해당 컨텍스트가 없는 절("규정"·"해결 사례")은 만들지 않는다. (5) 규정 문서가 둘 이상이면 차이점을 비교한다.
(6) 분량: 결론 1~2문장 → 근거 인용 → 필요하면 예외·조건 한 줄. 전체 5문장 이내(규정과 사례를 함께 쓸 때는 6문장), 목록은 항목당 한 줄. 수치·기간은 생략하지 않는다.
(7) 사례 줄 끝의 "(규정 관계: …)"는 그 조치와 규정의 관계를 알려 주는 자료다. 괄호를 그대로 옮겨 적지 않는다. 사례로 답할 때는 사례 문장마다 [N…] 마커를 붙이고, 이어서 규정 관계를 한 문장으로 쓴다 — 따른 조치면 "이 조치는 ○○ 제○조 기준에 따른 것이다 [P…]", 예외면 "당시에는 규정과 달리 …했다 [N…][P…]", 기준이 없으면 "이 부분은 규정에 기준이 없다 [N…]".
(8) 질문 뒤에 [답변에 꼭 담을 것]이 있으면 그 항목을 빠짐없이 답변에 담는다. 항목의 문장을 그대로 베끼지 말고 답변의 흐름에 맞게 쓴다."""


async def generate(profile: LlmProfile, query: str, policy_ctx: list[dict], chain_ctx: list[dict], usage: Usage) -> AsyncIterator[str]:
    """policy_ctx: {ref, article_label, doc_title, text}, chain_ctx: {ref, type, status, title, resolution_summary, depth}"""
    if ai_mode() == "openai" and profile.has_key:
        context = _render_context(policy_ctx, chain_ctx)
        must = _must_include(chain_ctx)
        tail = ("\n\n[답변에 꼭 담을 것]\n" + "\n".join(f"- {m}" for m in must)) if must else ""
        async for piece in _chat_stream(profile, profile.model_answer, GENERATE_SYSTEM, f"[컨텍스트]\n{context}\n\n[질문]\n{query}{tail}", usage,
                                        max_tokens=answer_max_tokens()):
            yield piece
        return
    # fake: 템플릿 답변 — 검색 결과를 그대로 마커와 함께 서술
    for piece in _template_answer(query, policy_ctx, chain_ctx):
        yield piece


def _template_answer(query: str, policy_ctx: list[dict], chain_ctx: list[dict]) -> list[str]:
    parts: list[str] = []
    if chain_ctx:
        fails = [n for n in chain_ctx if n["type"] == "fail"]
        causes = [n for n in chain_ctx if n["type"] == "ref"]
        fixes = [n for n in chain_ctx if n["type"] == "success" and n["status"] == "active"]
        if fails:
            parts.append(f"유사 사례: {fails[0]['title']} [{fails[0]['ref']}]. ")
        if causes:
            parts.append("원인: " + ", ".join(f"{c['title']} [{c['ref']}]" for c in causes[:2]) + ". ")
        if fixes:
            parts.append("해결: " + ", ".join(f"{f['resolution_summary'] or f['title']} [{f['ref']}]" for f in fixes[:2]) + ". ")
    if policy_ctx:
        parts.append("규정 근거: " + " ".join(f"{p['doc_title']} {p['article_label']} — {p['text'][:80].rstrip()}… [{p['ref']}]" for p in policy_ctx[:3]))
    if not parts:
        parts.append("근거 없음: 현재 워크스페이스에서 관련 규정이나 해결 사례를 찾지 못했습니다.")
    # 토큰처럼 보이게 어절 단위로 쪼갠다
    text = "".join(parts)
    return [w + " " for w in text.split(" ") if w]


def _must_include(chain_ctx: list[dict]) -> list[str]:
    """사례가 있을 때 답변이 빠뜨리기 쉬운 것을 컨텍스트에서 뽑는다 — 규칙만 주면 규정 문장만 쓰고 사례·예외를 건너뛰는 일이 잦았다.
    사례 인용, 규정과 달리 처리한 예외(체인 어디에 있든), 진입 사례의 '기준 없음', 진입 사례가 따른 규정."""
    live = [n for n in chain_ctx if n["status"] == "active"]
    if not live:
        return []
    out = ["해결 사례를 한 문장 이상 쓰고 그 문장에 [N…] 마커를 붙인다."]
    for n in live:
        for p in n.get("policy") or []:
            if p["relation"] == "exception":
                out.append(f"[{n['ref']}] 은 당시 규정([{p['ref']}])과 달리 처리한 사례다. 당시에 어떻게 했는지와 지금의 기준을 함께 쓰고 [{n['ref']}][{p['ref']}] 를 붙인다."
                           + (f" ({p['note']})" if p.get("note") else ""))
    entry = next((n for n in live if n.get("entry")), None)
    for p in (entry or {}).get("policy") or []:
        if p["relation"] == "gap":
            out.append(f"[{entry['ref']}] 이 다루는 부분은 규정에 기준이 없다고 알린다 — {p.get('topic') or ''}.")
        elif p["relation"] == "follows":
            out.append(f"[{entry['ref']}] 의 조치가 따른 규정 [{p['ref']}] 을 한 문장으로 밝힌다.")
    return out[:5]


def _policy_note(p: dict) -> str:
    """사례 줄 끝에 붙는 근거 표시(모델에게 주는 자료). 답변에 그대로 옮기지 않게 지시문 (7)이 문장 형태를 정한다."""
    if p["relation"] == "gap":
        return f" (규정 관계: 이 부분은 규정에 기준이 없다 — {p.get('topic') or p.get('note') or ''})"
    kind = "규정과 달리 처리한 예외" if p["relation"] == "exception" else "규정을 따른 조치"
    tail = f". {p['note']}" if p.get("note") else ""
    return f" (규정 관계: {p['ref']} {p.get('label') or ''} — {kind}{tail})"


def _render_context(policy_ctx: list[dict], chain_ctx: list[dict]) -> str:
    lines = []
    if policy_ctx:
        lines.append("## 규정")
        for p in policy_ctx:
            lines.append(f"[{p['ref']}] {p['doc_title']} {p['article_label']}: {p['text']}")
    if chain_ctx:
        lines.append("## 해결 사례 체인 (들여쓰기 = 상하 관계)")
        for n in chain_ctx:
            dep = "[폐기됨] " if n["status"] != "active" else ""
            indent = "  " * max(0, n["depth"])
            lines.append(f"{indent}[{n['ref']}] ({n['type']}) {dep}{n['title']}" + (f" — {n['resolution_summary']}" if n.get("resolution_summary") else "")
                         + "".join(_policy_note(p) for p in n.get("policy") or []))
    return "\n".join(lines)


# ── 정제 ─────────────────────────────────────────────────────────────────────

async def refine(profile: LlmProfile, title: str, body: str, tokenizer: Tokenizer, usage: Usage) -> RefineResult:
    text = f"{title}\n{body}"
    code = extract_error_code(text)
    if ai_mode() == "openai" and profile.has_key:
        system = ("Extract from a Korean work log: error_code (string or null, e.g. E-403, HTTP 504), keywords (3-6 short Korean/English terms), "
                  "resolution_summary (one Korean sentence of how it was resolved, or null). Reply JSON only.")
        data = await _chat_json(profile, profile.model_light, system, text, usage)
        try:
            r = RefineResult(**data)
            if not r.error_code and code:
                r.error_code = code
            return r
        except Exception:
            pass
    nouns = tokenizer.nouns(text, limit=6)
    first = re.split(r"(?<=[.。!?])\s+|\n", body.strip())[0] if body else None
    return RefineResult(error_code=code, keywords=nouns, resolution_summary=(first[:120] if first else None))


# ── OpenAI 호환 호출 ─────────────────────────────────────────────────────────

async def _chat_json(profile: LlmProfile, model: str, system: str, user: str, usage: Usage) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{profile.base_url}/chat/completions",
                              headers={"Authorization": f"Bearer {profile.api_key}"},
                              json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        j = r.json()
        u = j.get("usage") or {}
        usage.prompt_tokens += int(u.get("prompt_tokens", 0))
        usage.completion_tokens += int(u.get("completion_tokens", 0))
        return json.loads(j["choices"][0]["message"]["content"])


async def _chat_stream(profile: LlmProfile, model: str, system: str, user: str, usage: Usage,
                       max_tokens: int | None = None) -> AsyncIterator[str]:
    body = {"model": model, "temperature": 0.2, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if max_tokens:
        body["max_tokens"] = max_tokens
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{profile.base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {profile.api_key}"}, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                j = json.loads(payload)
                if j.get("usage"):
                    usage.prompt_tokens += int(j["usage"].get("prompt_tokens", 0))
                    usage.completion_tokens += int(j["usage"].get("completion_tokens", 0))
                for ch in j.get("choices", []):
                    if ch.get("finish_reason"):
                        usage.finish_reason = ch["finish_reason"]
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        yield delta
