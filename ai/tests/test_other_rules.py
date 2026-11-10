"""답변이 근거에 없는 다른 규정 영역을 안내 — 검색이 둘째 문서를 못 찾는 질의(80위 밖)를 위해.
발동 판정(_other_rule)과 서버가 답변 끝에 덧붙이는 고정 문구(other_rule_notice). 모델 입력은 바꾸지 않는다."""
from app.llm.client import NOTICE_PREFIX, _other_rule, _render_context, other_rule_notice

SCOPE = [{"slug": "retail-cs-protection-2026-11", "title": "고객응대 근로자 보호 지침"},
         {"slug": "retail-payroll-2026-11", "title": "급여 규정"},
         {"slug": "retail-privacy-2026-11", "title": "개인정보 처리 기준"},
         {"slug": "retail-privacy-plan-2026-11", "title": "개인정보 보호 내부관리계획"},
         {"slug": "retail-infosec-2026-11", "title": "정보보안 기본규정"},
         {"slug": "retail-recruit-2026-11", "title": "채용 절차와 채용서류 관리 지침"}]


def pol(*slugs):
    return [{"slug": s} for s in slugs]


def test_points_to_the_missing_area_by_real_title():
    o = _other_rule("폭언 때문에 상담 끊고 쉰 30분도 근무시간이야?", pol("retail-cs-protection-2026-11"), SCOPE)
    assert o == {"cue": "근무시간", "area": "급여·근로시간", "names": "「급여 규정」"}
    o = _other_rule("고객 연락처 든 파일 외부 업체에 보내도 되요?", pol("retail-privacy-2026-11", "retail-privacy-plan-2026-11"), SCOPE)
    assert o["names"] == "「정보보안 기본규정」"   # 개인정보 문서는 이미 근거에 있다 → 정보보안만
    o = _other_rule("급여 명세서를 팀장이 볼 수 있어?", pol("retail-payroll-2026-11"), SCOPE)
    assert o["names"] == "「개인정보 처리 기준」 · 「개인정보 보호 내부관리계획」"


def test_notice_is_fixed_wording_and_model_input_is_unchanged():
    policy = [{"ref": "P1", "doc_title": "고객응대 근로자 보호 지침", "article_label": "제12조", "text": "보호 조치", "slug": "retail-cs-protection-2026-11"}]
    n = other_rule_notice("폭언 때문에 상담 끊고 쉰 30분도 근무시간이야?", policy, SCOPE)
    assert n == "※ 급여·근로시간 기준은 「급여 규정」도 함께 확인해야 합니다." and n.startswith(NOTICE_PREFIX)
    assert _render_context(policy, []) == "## 규정\n[P1] 고객응대 근로자 보호 지침 제12조: 보호 조치"   # 모델이 보는 근거는 그대로
    assert other_rule_notice("폭언 때문에 쉰 30분도 근무시간이야?", [], SCOPE) is None   # 근거가 없으면(근거 없음 답변) 붙이지 않는다


def test_no_note_when_area_is_already_in_evidence_or_not_in_scope():
    assert _other_rule("야근수당은 어떻게 계산해?", pol("retail-payroll-2026-11"), SCOPE) is None
    assert _other_rule("폭언 때문에 상담 끊고 쉰 30분도 근무시간이야?", pol("it-refund-2026-11"),
                       [{"slug": "it-refund-2026-11", "title": "환불 규정"}]) is None   # 그 영역 문서가 범위에 없다
    assert _other_rule("폭언 때문에 쉰 시간도 근무시간이야?", pol("retail-cs-protection-2026-11"), None) is None


def test_personal_data_needs_an_access_or_transfer_word():
    # 낱말만으로 걸리던 질문(317건 중 4건) — 지급 시기 · 정정 · 허위 기재 · 본인 확인
    for q in ("급여명세서 언제까지 줘야되는거임", "이력서 이미 낸 사람이 내용 바꾸고 싶다는데 되요?",
              "이력서에 거짓말 쓴 게 들통나면 어떻게 처리해?", "연락처 바뀐 고객은 본인 확인 전에 뭘 먼저 해야 해요?"):
        assert _other_rule(q, pol("retail-recruit-2026-11"), SCOPE) is None, q
