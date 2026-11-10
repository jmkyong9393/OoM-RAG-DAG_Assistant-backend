"""다른 규정 안내 — 파이프라인 끝까지(로컬 Postgres + fake LLM). 붙는 안내가 실제 근거 · 부서 범위로 계산한 값과 정확히 같은지,
규정을 끈 실행에서는 붙지 않는지. 검색 결과는 데이터에 따라 달라지므로 '근거를 보고 계산한 기대값'과 비교한다."""
import re

import pytest

from app.graph import pipeline
from app.llm.client import NOTICE_PREFIX, other_rule_notice
from app.retrieval import db
from tests.conftest import collect, requires_db

pytestmark = requires_db

CASES = [  # (부서, 질문) — 유통 · IT, 영역별 단서
    (3, "폭언 때문에 상담 끊고 쉰 30분도 근무시간이야?"),
    (10, "고객 연락처 든 파일 외부 업체에 보내도 되요?"),
    (2, "급여 명세서를 팀장이 볼 수 있어?"),
    (2, "급여 명세서를 외부 업체에 보내도 돼?"),
    (1, "개인정보 다운로드 기능 만들 때 뭘 남겨야 해?"),
    (23, "환불 처리하다가 고객 연락처를 외부로 보내도 되나요?"),
    (21, "장애 대응하느라 야근한 시간도 수당 나와?"),
    (3, "영수증 잃어버렸는데 환불 되나요"),
]
SLUG = re.compile(r"-[aqps]\d+(-\d+)?-\d+$")


def tokens(blocks) -> str:
    return "".join(b[1]["text"] for b in blocks if b[0] == "token")


@pytest.mark.parametrize("ws,query", CASES)
async def test_notice_equals_what_the_evidence_and_scope_imply(fake_mode, backfilled, ws, query):
    blocks = await collect(pipeline.run(ws, True, query))
    text = tokens(blocks)
    sent = [b[1] for b in blocks if b[0] == "policy"]   # 사례만 찾다 대체 검색을 하면 두 번 — 최종 근거는 마지막 것
    policies = [{"slug": SLUG.sub("", p["chunk_id"])} for p in (sent[-1] if sent else [])]
    with db.connect() as conn:
        scope = db.scope_documents(conn, ws)
    head, sep, tail = text.rpartition("\n\n" + NOTICE_PREFIX)
    body, got = (head, NOTICE_PREFIX + tail) if sep else (text, None)
    assert got == other_rule_notice(query, policies, scope, body), (ws, query, [p["slug"] for p in policies])
    assert text.count(NOTICE_PREFIX) <= 1, "안내는 많아야 한 줄"


async def test_no_notice_when_policy_is_off(fake_mode, backfilled):
    blocks = await collect(pipeline.run(3, False, "폭언 때문에 상담 끊고 쉰 30분도 근무시간이야?"))
    assert NOTICE_PREFIX not in tokens(blocks)
