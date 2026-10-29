"""LLM 계층의 fake 모드(규칙 기반) 단위 테스트. OpenAI 호출 없이 분류·생성·정제·컨텍스트 렌더링을 검증한다."""
import pytest

from app.config import profile
from app.llm import client as llm
from app.retrieval.tokenize import Tokenizer


@pytest.fixture
def prof(fake_mode):
    return profile()


@pytest.mark.parametrize("query,policy_enabled,expected", [
    ("VPN 원격접속 신청 기준이 뭐야", True, "policy"),
    ("E-403 오류 예전에 어떻게 해결했어", True, "troubleshoot"),
    ("VPN 에러가 나는데 규정상 어떻게 해야 해", True, "both"),
    ("오늘 점심 뭐 먹지", True, "both"),            # 애매하면 both
    ("VPN 원격접속 신청 기준이 뭐야", False, "troubleshoot"),  # dag_only 는 항상 troubleshoot
])
async def test_classify_rules(prof, query, policy_enabled, expected):
    from app.retrieval.tokenize import extract_error_code
    r = await llm.classify(prof, query, extract_error_code(query), policy_enabled, llm.Usage())
    assert r.intent == expected


async def test_template_answer_uses_markers_and_skips_deprecated(prof):
    chain = [
        {"ref": "N1", "id": 101, "type": "fail", "status": "active", "title": "VPN E-403", "resolution_summary": None, "depth": 0},
        {"ref": "N2", "id": 106, "type": "ref", "status": "active", "title": "토큰 갱신 배치 누락", "resolution_summary": None, "depth": 1},
        {"ref": "N3", "id": 109, "type": "success", "status": "deprecated", "title": "수동 재발급", "resolution_summary": "수동 스크립트", "depth": 2},
        {"ref": "N4", "id": 108, "type": "success", "status": "active", "title": "갱신 주기 조정", "resolution_summary": "주기 30분", "depth": 2},
    ]
    policy = [{"ref": "P1", "chunk_id": "x", "doc_title": "VPN 지침", "article_label": "제12조", "text": "사전 신청"}]
    pieces = [p async for p in llm.generate(prof, "q", policy, chain, llm.Usage())]
    answer = "".join(pieces)
    assert "[N1]" in answer and "[N2]" in answer and "[N4]" in answer and "[P1]" in answer
    assert "[N3]" not in answer, "폐기된 success 는 권장하지 않는다"
    assert len(pieces) > 5, "토큰처럼 여러 조각으로 흘러야 한다"


async def test_template_answer_no_evidence(prof):
    answer = "".join([p async for p in llm.generate(prof, "q", [], [], llm.Usage())])
    assert answer.startswith("근거 없음")


async def test_refine_fake_extracts_code_keywords_summary(prof):
    r = await llm.refine(prof, "FAQ 연동 API 타임아웃(504)", "FAQ 연동 API 호출이 HTTP 504 로 실패. 커넥션 풀을 늘려 해결했다.", Tokenizer(), llm.Usage())
    assert r.error_code == "HTTP 504"
    assert r.keywords and all(len(k) > 1 for k in r.keywords)
    assert r.resolution_summary and r.resolution_summary.startswith("FAQ 연동 API 호출이")


def test_render_context_marks_deprecated_and_indents():
    ctx = llm._render_context(
        [{"ref": "P1", "doc_title": "D", "article_label": "제1조", "text": "t"}],
        [{"ref": "N1", "type": "fail", "status": "active", "title": "a", "depth": 0},
         {"ref": "N2", "type": "success", "status": "deprecated", "title": "b", "depth": 1, "resolution_summary": "s"}])
    assert "[P1] D 제1조: t" in ctx
    assert "  [N2] (success) [폐기됨] b — s" in ctx
