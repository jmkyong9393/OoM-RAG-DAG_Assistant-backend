"""규칙 분류(rule_classify)를 평가셋 전체 질의로 검증. DB·LLM 불필요.

기준: 평가셋의 kind(policy|troubleshoot)와 어긋나는 단일 의도를 내면 안 된다(policy 질문 → troubleshoot 금지, 그 반대도).
'both' 와 None(애매 → LLM) 은 안전하다. 덤으로 LLM 없이 결정된 비율을 찍는다.
"""
import json
from pathlib import Path

import pytest

from app.llm.client import rule_classify
from app.retrieval.tokenize import extract_error_code

EVALSET = Path(__file__).resolve().parents[2] / "data" / "evalset"
ITEMS = [it for f in sorted(EVALSET.glob("*.json")) for it in json.loads(f.read_text(encoding="utf-8"))]


def test_rules_never_contradict_evalset_kind(capsys):
    assert len(ITEMS) >= 40
    wrong, decided = [], 0
    for it in ITEMS:
        r = rule_classify(it["query"], extract_error_code(it["query"]))
        if r is None:
            continue
        decided += 1
        if (it["kind"] == "policy" and r.intent == "troubleshoot") or (it["kind"] == "troubleshoot" and r.intent == "policy"):
            wrong.append((it["query"], r.intent, r.reason))
    assert wrong == [], f"규칙이 평가셋 kind 와 어긋남: {wrong}"
    with capsys.disabled():
        print(f"\n[intent rules] LLM 없이 결정 {decided}/{len(ITEMS)} ({decided * 100 // len(ITEMS)}%)")
    assert decided >= len(ITEMS) // 2, "절반 이상은 규칙으로 끝나야 분류 LLM 호출(≈4 s)을 체감되게 줄인다"


@pytest.mark.parametrize("query,code,expected", [
    ("스마트폰 품질보증기간이 얼마나 되나요", None, "policy"),
    ("E-403 뜨는데 어떻게 하지", "E-403", "troubleshoot"),
    ("VPN 규정상 E-403 에러는 어떻게 조치해?", "E-403", "both"),
    ("배포 후 504 timeout 이 재발해요", None, "troubleshoot"),
    ("요즘 VPN 이 왜 이래?", None, None),            # 애매 → LLM
    ("시간 동기화가 안 맞으면 인증이 실패하나요", None, None),  # '실패' 는 약한 힌트 — 규정 질문일 수 있어 LLM 에
    ("접속이 실패하는데 규정상 승인 기간이 지난 건가요", None, "both"),  # 약한 힌트 + 규정 힌트 → both
    ("VPN 접속이 거부될 때 어떻게 해야 해", None, None),  # '거부'는 힌트가 아니다 (규정 FAQ 일 수 있음). '해야 해' 단독도 LLM 에
    # 장애 힌트 + 의무/기준값 힌트 → both (규정 없이 사례 수치만으로 "기준" 을 답하지 않게)
    ("배포하고 나서 502가 늘어나는데 롤백해야 해?", None, "both"),
    ("컨테이너가 OOM으로 계속 재시작되는데 장애로 등록해야 해?", None, "both"),
    ("외부 API 타임아웃 몇 초로 잡고 재시도는 몇 번까지 해?", None, "both"),
    ("502 급증 났던 거 예전에 어떻게 해결했어?", None, "troubleshoot"),  # 순수 사례 질의는 그대로
    # 규정+사례형 부서의 규정 질문은 "장애"·"배치" 를 품는다 — 의무·허용·수량 어미가 있으면 both (규정 검색이 빠지지 않게)
    ("사후분석을 꼭 써야 하는 장애 등급이 뭐야?", None, "both"),
    ("Hard 동결 중에 장애 대응용 긴급 패치는 내보내도 돼?", None, "both"),
    ("장애 원인 자체를 뜯어고치는 일은 몇 주 안에 끝내야 돼?", None, "both"),
    ("1급 장애랑 3급 장애는 보고 시한이 어떻게 달라?", None, "both"),
    # 장애 낌새 없이 의무·수량만 묻는다 → policy (분류 LLM 호출 없이)
    ("비밀번호는 몇 자 이상으로 만들어야 해?", None, "policy"),
    ("운영 배포는 무슨 요일 몇 시에 할 수 있어?", None, "policy"),
    # '복구' 는 계정 복구에도 쓰이는 약한 힌트 — 단독으로는 LLM 에
    ("보안 키 잃어버린 고객 계정 복구는 어떤 절차로 해?", None, "both"),     # '절차' 규정 힌트 + 약한 힌트
    ("조직 계정 보조 이메일 복구는 어떤 규모 조직에서 못 써요?", None, None),
    # 경험을 묻는 표현은 사례 질의다. 규정 힌트와 함께 오면 both
    ("계정 잠겼을 때 어떻게 풀었어?", None, "troubleshoot"),
    ("커넥션풀 꽉찼을때 어케했어", None, "troubleshoot"),
    ("배포 전에 설정 검증하는 단계 만든 거", None, "troubleshoot"),
    ("롤백 기준 안 넘었는데 롤백한 적 있어?", None, "both"),
    ("JVM 힙을 컨테이너 제한 기준으로 얼마로 잡았어?", None, "both"),
    # 증상 호소는 조치 규정과 사례를 함께 본다
    ("앱이 로그인 직후에 자꾸 로그아웃돼요", None, "both"),
    ("스테이징 배포가 이미지를 못 받아와요", None, "both"),
    ("전자결재 상신이 안 돼요", None, "both"),
    ("날짜 지난 물건이 들어 있었어요", None, None),           # 고객 불만을 옮긴 말 — 증상·경험 표현이 아니다
    # 대문자 상수 모양의 오류 이름은 에러 코드처럼 사례 신호다
    ("휴대폰 결제 취소 CANCEL_PERIOD_EXPIRED 뜨는 이유", None, "both"),
    # 증상을 호소하면서 기준을 묻는 말 — 규정만 보지 않는다
    ("매칭이 몇 분씩 안 잡혀요", None, "both"),
    ("환불 신청한 지 일주일 넘었는데 안 들어온대요", None, "both"),
])
def test_rule_examples(query, code, expected):
    r = rule_classify(query, code)
    assert (r.intent if r else None) == expected



async def test_classify_uses_workspace_data(fake_mode):
    """부서 데이터로 하는 판단: 사례가 없으면 규정만, 에러 코드를 규정이 다루면 규정도 함께."""
    from app.config import profile
    from app.llm import client as llm
    prof, u = profile(), llm.Usage()
    r = await llm.classify(prof, "채용서류 반환 청구가 들어오면 어떻게 해?", None, True, u, has_cases=False)
    assert r.intent == "policy" and "no cases" in r.reason
    r = await llm.classify(prof, "E-403 뜨는데 어떻게 하지", "E-403", True, u, has_cases=True, code_in_policy=True)
    assert r.intent == "both" and "appears in policy" in r.reason
    r = await llm.classify(prof, "E-403 뜨는데 어떻게 하지", "E-403", True, u, has_cases=True, code_in_policy=False)
    assert r.intent == "troubleshoot"
    r = await llm.classify(prof, "E-403 뜨는데 어떻게 하지", "E-403", False, u, has_cases=False)
    assert r.intent == "troubleshoot" and r.reason == "dag_only mode"          # 규정을 끈 부서는 그대로 사례만
