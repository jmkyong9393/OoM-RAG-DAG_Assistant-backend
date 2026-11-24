"""결제수단을 붙인 취소 · 환불 질문: 위 조문이 그 수단을 따로 말하지 않을 때만 '그 기준이 그 수단에도 적용된다'고 짚는다."""
from app.llm import client

ORDER_CANCEL = {"ref": "P1", "article_label": "제6조 (주문 취소)", "text": "① 배송이 시작되기 전에는 주문을 취소하고, 배송이 시작된 뒤에는 반품 절차로 처리한다. "
                "④ 무통장입금(가상계좌) 주문은 2영업일 안에 입금하지 않으면 자동 취소된다."}
BY_MEANS = {"ref": "P1", "article_label": "제12조 (결제취소 기간)", "text": "① 신용카드는 결제일부터 60일 이내, 휴대전화 결제는 결제한 달 말일의 하루 전까지 결제취소한다."}
REFUND_BY_MEANS = {"ref": "P1", "article_label": "제16조 (결제수단별 환불)", "text": "② 신용카드 결제분은 카드 결제 취소로만 환불한다. ③ 회사 간편결제와 충전금 결제분은 당일, "
                   "신용·체크카드 결제분은 3~7일 안에 환불된다."}
LEAVE = {"ref": "P1", "article_label": "제3조 (연차)", "text": "① 1년간 80% 이상 출근한 직원에게 15일의 유급휴가를 준다."}


def _p2(p):
    return {**p, "ref": "P2"}


def test_points_to_the_general_rule_when_no_clause_names_the_means():
    notes = client._means_notes("휴대폰 결제 취소는 언제까지 되나요?", [ORDER_CANCEL])
    assert len(notes) == 1 and notes[0].startswith("[P1]") and "'휴대폰'" in notes[0], "한 항에서 무통장 하나를 말한 조문은 일반 기준이다"


def test_uses_the_word_from_the_question():
    notes = client._means_notes("오움페이로 결제한 주문은 언제까지 취소할 수 있어?", [ORDER_CANCEL])
    assert "'오움페이'" in notes[0] and "간편결제" not in notes[0], "질문에 쓴 말을 다른 이름으로 바꿔 부르지 않는다"


def test_silent_when_a_clause_already_sets_the_rule_for_that_means():
    assert client._means_notes("카드 결제랑 휴대폰 결제는 결제취소 되는 기간이 달라?", [BY_MEANS, _p2(ORDER_CANCEL)]) == [], \
        "수단별 기준이 있으면 그 조문이 답이다 — 다른 조문을 '수단과 관계없이 적용'이라고 짚지 않는다"


def test_skips_a_by_means_clause_that_lists_other_means():
    notes = client._means_notes("상품권으로 결제한 거 환불 되나요?", [REFUND_BY_MEANS, _p2(ORDER_CANCEL)])
    assert len(notes) == 1 and notes[0].startswith("[P2]"), "수단을 늘어놓은 조문(제16조)을 '나누지 않은 기준'이라 부르지 않는다"
    assert client._means_notes("상품권으로 결제한 거 환불 되나요?", [REFUND_BY_MEANS]) == []


def test_silent_without_means_or_without_cancel_refund():
    assert client._means_notes("주문 취소는 언제까지 되나요?", [ORDER_CANCEL]) == []
    assert client._means_notes("휴대폰 결제 취소는 언제까지 되나요?", [LEAVE]) == [], "취소 · 환불을 정한 조문이 없으면 짚지 않는다"


def test_card_words_that_are_not_a_payment_means():
    assert client._means_in("법인카드 사용 취소") == [] and client._means_in("기프트카드 환불") == []
    assert client._means_in("카드사 이의제기 환불") == [] and client._means_in("마이페이지에서 취소") == []
    assert client._means_in("체크카드로 산 물건") == ["체크카드"] and client._means_in("카드로 긁은 주문") == ["카드"]
