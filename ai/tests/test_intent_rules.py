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
    ("VPN 접속이 거부될 때 어떻게 해야 해", None, None),  # '거부'는 힌트가 아니다 (규정 FAQ 일 수 있음)
])
def test_rule_examples(query, code, expected):
    r = rule_classify(query, code)
    assert (r.intent if r else None) == expected
