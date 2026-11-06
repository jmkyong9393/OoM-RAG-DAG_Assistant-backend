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
SYMPTOM_RE = re.compile(r"(?:자꾸|계속|갑자기|반복해서)\s?[^?]{0,20}(?:돼요|되요|돼|됨|겨요|져요|떠요|뜸|나요|남)|못\s?[가-힣]{1,6}(?:와요|해요|가요|옴|함)|안\s?(?:돼요|됩니다|되네|잡혀|들어와|들어온|와요|나와요|떠요|열려|보여요)")
CODE_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")   # 'CANCEL_PERIOD_EXPIRED' — 대문자 상수 모양의 오류 이름
HTTP_STATUS_RE = re.compile(r"(?<!\d)[45]\d\d(?!\d)")   # '504', '429로', '403 떨어지는데' — 접두어 없는 HTTP 상태 코드


def rule_classify(query: str, error_code: str | None) -> IntentResult | None:
    """힌트만으로 결정되면 IntentResult, 애매하면 None. 에러 코드는 troubleshoot 의 강한 신호."""
    q = query.lower()
    has_policy = any(h in q for h in POLICY_HINTS)
    has_trouble = (bool(error_code) or any(h in q for h in TROUBLE_HINTS) or bool(HTTP_STATUS_RE.search(q)) or bool(EXPERIENCE_RE.search(q))
                   or bool(CODE_NAME_RE.search(query)))
    symptom = bool(SYMPTOM_RE.search(q))
    weak_trouble = any(h in q for h in TROUBLE_WEAK_HINTS)
    weak_policy = any(h in q for h in POLICY_WEAK_HINTS) or bool(OBLIGATION_RE.search(q))
    if has_policy and (has_trouble or weak_trouble):
        return IntentResult(intent="both", reason="rule: policy+trouble hints")
    if has_trouble and weak_policy:
        return IntentResult(intent="both", reason="rule: trouble + obligation/threshold hints")
    if symptom and (has_policy or weak_policy) and not has_trouble:   # 증상을 호소하면서 기준을 묻는다 → 둘 다
        return IntentResult(intent="both", reason="rule: symptom + policy hints")
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
규칙: (1) 인용은 제공된 마커([P1], [N2] 등)만 쓴다. (2) 사실을 담은 모든 문장과 표의 모든 행 끝에 근거 마커를 붙인다. 마커는 그 내용이 실제로 적힌 줄의 것만 쓴다(해결 방법은 그 해결이 적힌 줄의 마커). 컨텍스트에 없는 내용은 "근거 없음"이라고 말하고 일반 상식으로 채우지 않는다.
(3) [폐기됨] 표시 노드는 권장하지 않는다. (4) 규정 근거와 해결 사례를 구분해 쓴다. 단, 해당 컨텍스트가 없는 절("규정"·"해결 사례")은 만들지 않는다. (5) 규정 문서가 둘 이상이면 차이점을 비교한다. 원칙과 예외·단서(다만, 긴급, 승인을 받으면 등)가 함께 있으면 결론 첫 문장에 둘을 함께 쓴다 — "원칙적으로 …이나, …이면 …할 수 있다". 예외가 있는데 "할 수 없다"고만 쓰지 않는다. 근거에 원칙을 정한 문장이 없으면 "원칙적으로"라고 쓰지 않는다.
(6) 분량: 결론 1~2문장 → 근거 인용 → 필요하면 예외·조건 한 줄. 전체 5문장 이내(규정과 사례를 함께 쓸 때는 6문장), 목록은 항목당 한 줄. 수치·기간은 생략하지 않는다.
(7) 사례 줄 끝의 "(규정 관계: …)"는 그 조치와 규정의 관계를 알려 주는 자료다. 괄호를 그대로 옮겨 적지 않는다. 사례로 답할 때는 사례 문장마다 [N…] 마커를 붙이고, 이어서 규정 관계를 한 문장으로 쓴다 — 따른 조치면 "이 조치는 ○○ 제○조 기준에 따른 것이다 [P…]", 예외면 "당시에는 규정과 달리 …했다 [N…][P…]", 기준이 없으면 "이 부분은 규정에 기준이 없다 [N…]".
(8) 질문 뒤에 [답변에 꼭 담을 것]이 있으면 그 항목을 빠짐없이 답변에 담는다. 항목의 문장을 그대로 베끼지 말고 답변의 흐름에 맞게 쓴다.
(9) 질문이 묻는 대상(예: 연차, 특정 수당·절차·시스템)이 근거에 아예 적혀 있지 않으면 가능·불가능을 추측하지 말고 "제공된 규정에는 …에 관한 기준이 없습니다"라고 답한다. 주제가 비슷한 조문이 있어도 그 대상을 말하지 않으면 없는 것이다. 조문이 정한 조건과 질문의 상황이 서로 다른 것이면(예: 조문은 '기한 경과'를 정했는데 질문은 '기한 임박') 첫 문장에서 가능·불가능으로 단정하지 말고 그 상황을 직접 정한 기준은 없다고 먼저 밝힌다. 이어서 각 조문이 정한 조건과 결과를 조문에 쓰인 말 그대로 "…인 경우에는 …" 꼴로 나누어 답하고, 질문의 상황이 그 조건에 해당한다고 스스로 판정하지 않는다("하자로 간주된다", "대상이 아니다", "불가능하다", "가능하다" 같은 단정을 쓰지 않는다). 조문에 없는 일반론("일반적으로 …", 법령 상식)이나 조문에서 미루어 짐작한 결론을 덧붙이지 않는다 — 근거에 없으면 그 부분을 정한 규정이 없다고 답한다."""


async def generate(profile: LlmProfile, query: str, policy_ctx: list[dict], chain_ctx: list[dict], usage: Usage,
                   case_required: bool = True) -> AsyncIterator[str]:
    """policy_ctx: {ref, article_label, doc_title, text}, chain_ctx: {ref, type, status, title, resolution_summary, depth}"""
    if ai_mode() == "openai" and profile.has_key:
        context = _render_context(policy_ctx, chain_ctx)
        # 질문이 겨냥한 조문 = 검색 상위 2건. 그 아래 순위의 조문을 벗어난 예외까지 강제하면 질문과 먼 사례가 답에 섞인다
        searched = [x for x in policy_ctx if x.get("source") == "search"]
        must = _must_include(chain_ctx, case_required, {p["ref"] for p in searched[:2]}) + _wording_notes(searched[:3])
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
        fixes = sorted((n for n in chain_ctx if n["type"] == "success" and n["status"] == "active"), key=lambda n: not n.get("resolves_root"))
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


def _must_include(chain_ctx: list[dict], case_required: bool = True, searched: set[str] | None = None) -> list[str]:
    """사례가 있을 때 답변이 빠뜨리기 쉬운 것을 컨텍스트에서 뽑는다 — 규칙만 주면 규정 문장만 쓰고 사례·예외를 건너뛰는 일이 잦았다.
    사례 인용, 규정과 달리 처리한 예외, 진입 사례의 '기준 없음', 진입 사례가 따른 규정.
    예외는 질문과 이어진 것만: 진입 사례와 같은 줄기(near)이거나, 질문으로 검색된 조문(searched)을 벗어난 사례. 같은 체인의 다른 가지에 있는 예외까지 넣으면 답이 샌다."""
    live = [n for n in chain_ctx if n["status"] == "active"]
    if not live:
        return []
    # 규정을 물은 질문에 딸려 온 사례는 참고다 — 질문과 닿을 때만 쓰게 한다 (억지로 인용시키면 주제만 비슷한 사례가 답에 섞인다)
    # 장애 건을 실제로 해결한 기록이 남아 있으면 그 줄을 앞에 — 같은 줄기의 다른 해결(후속 절차·부분 조치)과 섞지 않게
    fix_lines = sorted((n for n in live if n["type"] == "success" and (n.get("entry") or n.get("near"))), key=lambda n: not n.get("resolves_root"))
    fixes = [n["ref"] for n in fix_lines]
    solved = [n["ref"] for n in fix_lines if n.get("resolves_root")]
    if solved:
        others = [r for r in fixes if r not in solved]
        cite = (f"해결 사례를 한 문장 이상 쓴다. 이 장애를 실제로 해결한 기록은 {' '.join(f'[{r}]' for r in solved[:4])} 이다 — 해결 방법은 이 줄에서 먼저 쓰고 그 줄의 마커를 붙인다."
                + (f" {' '.join(f'[{r}]' for r in others[:3])} 은 해결로 기록되지 않은 조치이니 쓰려면 후속·보조 조치로 구분한다." if others else ""))
    else:
        cite = (f"해결 사례를 한 문장 이상 쓴다. 해결 방법은 해결 줄({' '.join(f'[{r}]' for r in fixes[:4])}) 가운데 질문에 맞는 것에서 쓰고 그 줄의 마커를 붙인다."
                if fixes else "해결 사례를 한 문장 이상 쓰고 그 문장에 [N…] 마커를 붙인다.")
    out = [cite if case_required else
           "먼저 규정으로 답한다. 해결 사례가 이 질문의 상황을 실제로 다룬 것이면 한 문장으로 덧붙이고 [N…] 마커를 붙인다. 상황이 다르면 사례는 쓰지 않고, 사례가 없다거나 다루지 않는다는 말도 하지 않는다."]
    for n in live:
        for p in n.get("policy") or []:
            if p["relation"] == "exception" and p.get("ref") and (n.get("entry") or n.get("near") or p["ref"] in (searched or ())):
                out.append(f"[{n['ref']}] 은 당시 규정([{p['ref']}])과 달리 처리한 사례다. 당시에 어떻게 했는지와 지금의 기준을 함께 쓰고 [{n['ref']}][{p['ref']}] 를 붙인다."
                           + (f" ({_note_text(p)})" if p.get("note") else ""))
    # 규정이 개정된 사례(진입 사례와 같은 줄기만): 지금 기준과 다를 수 있다는 것을 빠뜨리면 옛 기준을 현재 기준처럼 답한다. 사례가 여럿이어도 한 줄로
    line = [n for n in live if n.get("entry") or n.get("near")]
    amended = [(n, p) for n in line for p in n.get("policy") or [] if p.get("revision") == "amended"]
    replaced = [(n, p) for n in line for p in n.get("policy") or [] if p.get("revision") == "replaced" and p["relation"] != "gap"]
    if amended:
        refs = " ".join(dict.fromkeys(f"[{p['ref']}]" for _, p in amended))
        ver = amended[0][1].get("from_version")
        out.append(f"이 사례들은 규정이 개정되기 전({ver} 판)에 처리한 것이다. '규정이 그 뒤 개정됐다'는 것을 한 문장으로 밝히고, 지금 기준은 조문 {refs} 에 적힌 값으로 쓴다. "
                   f"메모의 '당시({ver} 판) 기준'은 개정 전 규정의 값이다 — 사례에서 쓴 값, 개정 전 기준, 지금 기준을 섞지 말고 구분해 쓴다.")
    if replaced:
        n, p = next(((n, p) for n, p in replaced if n.get("entry")), replaced[0])
        how = "과 달리 한" if p["relation"] == "exception" else "에 따른"
        out.append(f"[{n['ref']}] 은 개정 전 규정({p.get('label')}){how} 처리다. 그 규정은 개정됐으니, 사례의 처리 방식이 지금 기준과 다를 수 있다고 알리고 "
                   "지금 기준은 ## 규정 의 조문으로 답한다.")
    entry = next((n for n in live if n.get("entry")), None)
    for p in (entry or {}).get("policy") or []:
        if p["relation"] == "gap" and p.get("revision"):
            out.append(f"[{entry['ref']}] 을 처리할 당시에는 이 부분({p.get('topic') or '이 조치'})을 정한 규정이 없었다. 그 뒤 규정이 개정됐으니 "
                       "'규정에 기준이 없다'고 단정하지 말고, 지금 기준은 ## 규정 의 조문으로 답한다.")
        elif p["relation"] == "gap":
            out.append(f"[{entry['ref']}] 이 다루는 부분({p.get('topic') or '이 조치'})은 회사 규정에 기준이 없다. "
                       f"'이 부분은 규정에 기준이 없다'는 문장을 답변에 넣고 [{entry['ref']}] 를 붙인다.")
        elif p["relation"] == "follows" and p.get("ref") and (entry["type"] == "success" or not fixes):
            out.append(f"[{entry['ref']}] 의 조치({_action(entry)})는 규정 [{p['ref']}] 을 따른 것이라고 한 문장으로 밝힌다. 규정의 기준 자체를 이 조치로 설명하지 않는다.")
    if entry and entry["type"] != "success" and fixes and case_required:
        # 해결 줄마다 규정 관계를 짚어 준다 — "규정 관계가 있으면 밝혀라"로만 쓰면 다른 줄의 [P…] 를 기준 없는 조치에 붙인다
        line = _fix_relations([n for n in live if n["ref"] in fixes])
        if line:
            out.append(line)
    return out[:6]


def _action(n: dict) -> str:
    """사례 줄의 조치 이름: 제목에서 '해결:' 같은 머리를 뗀다."""
    t = n.get("title") or ""
    for head in ("해결(구):", "해결:", "원인:", "참고:"):
        if t.startswith(head):
            t = t[len(head):]
    t = t.strip()
    return t if len(t) <= 40 else t[:40] + "…"


def _fix_relations(fixes: list[dict]) -> str:
    """해결 줄별 규정 관계 한 줄. 표시가 없는 줄은 뺀다(근거 없는 줄에 [P…] 를 붙이라고 하면 없는 마커를 지어낸다)."""
    parts = []
    for n in fixes[:4]:
        rel = []
        for p in n.get("policy") or []:
            if p.get("revision") == "replaced":
                continue
            if p["relation"] == "gap":
                rel.append("규정에 기준이 없다")
            elif p.get("ref"):
                rel.append(f"[{p['ref']}] 을 따른 조치" if p["relation"] == "follows" else f"[{p['ref']}] 과 달리 처리")
        if rel:
            parts.append(f"[{n['ref']}] {_action(n)} → {' · '.join(dict.fromkeys(rel))}")
    if not parts:
        return ""
    return ("해결 줄의 규정 관계는 줄마다 다르다 — " + " / ".join(parts)
            + ". 답변에 쓴 해결 줄의 관계만 그 줄의 마커와 함께 한 문장으로 밝히고, 다른 줄의 규정을 이 조치에 붙이지 않는다.")


# 조문이 의무가 아니라 목표·노력·권장으로 정한 표현. 모델이 질문의 "…해야 해?"에 끌려 의무로 바꿔 쓴다
SOFT_WORDING = (("목표로", "목표"), ("노력한다", "노력"), ("노력하여야", "노력"), ("권장한다", "권장"), ("권고한다", "권고"))


def _wording_notes(policies: list[dict]) -> list[str]:
    """질문이 겨냥한 조문(검색 상위)이 기준을 목표·노력·권장으로 정했으면, 의무로 바꿔 쓰지 말라고 짚는다."""
    out = []
    for p in policies:
        found = [w for k, w in SOFT_WORDING if k in (p.get("text") or "")]
        if found:
            w = found[0]
            out.append(f"[{p['ref']}] 은 이 기준을 의무가 아니라 '{w}'(으)로 정했다. '…해야 한다'로 바꾸지 말고 '{w}'라는 말을 그대로 쓴다.")
    return out[:2]


def _note_text(p: dict) -> str:
    """대조표의 메모. 규정이 개정된 사례면 메모가 말하는 '현재 기준'은 개정 전 기준이다 — 모델에게 바꿔 읽으라고 하지 않고 여기서 바꿔 넘긴다."""
    note = p.get("note") or ""
    if note and p.get("revision"):
        ver = f"{p['from_version']} 판" if p.get("from_version") else "개정 전"
        return "개정 전에 적은 메모 — " + note.replace("현재 기준", f"당시({ver}) 기준")
    return note


def _policy_note(p: dict) -> str:
    """사례 줄 끝에 붙는 근거 표시(모델에게 주는 자료). 답변에 그대로 옮기지 않게 지시문 (7)이 문장 형태를 정한다."""
    if p["relation"] == "gap" and p.get("revision"):
        return f" (규정 관계: 처리 당시에는 규정에 기준이 없었다 — {p.get('topic') or p.get('note') or ''}. 그 뒤 규정이 개정됐다)"
    if p["relation"] == "gap":
        return f" (규정 관계: 이 부분은 규정에 기준이 없다 — {p.get('topic') or p.get('note') or ''})"
    kind = "규정과 달리 처리한 예외" if p["relation"] == "exception" else "규정을 따른 조치"
    tail = f". {_note_text(p)}" if p.get("note") else ""
    if p.get("revision") == "replaced":   # 근거로 실리지 않는다(마커 없음) — 개정 전 기준이었다는 사실만
        return f" (규정 관계: 개정 전 규정 {p.get('label') or ''} 기준의 처리 — {kind}{tail}. 이 규정은 그 뒤 개정됐고 지금 대응하는 조문은 정해지지 않았다)"
    rev = f". 이 조문은 그 뒤 개정됐다 — 사례는 {p.get('from_version')} 판 기준이다" if p.get("revision") == "amended" else ""
    return f" (규정 관계: {p['ref']} {p.get('label') or ''} — {kind}{tail}{rev})"


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
                         + (" (이 장애를 해결한 기록)" if n.get("resolves_root") and n["status"] == "active" else "")
                         + "".join(f" (상위에 이은 이유: {r})" for r in n.get("via") or [])
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
    body = {"model": model, "temperature": 0, "stream": True, "stream_options": {"include_usage": True},
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
