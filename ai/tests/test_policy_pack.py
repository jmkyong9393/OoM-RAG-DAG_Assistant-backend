"""규정 묶음 점검(scripts/validate_policy_pack.py)과 규정↔사례 대조표(scripts/check_alignment.py). DB 불필요.

앞쪽은 임시 폴더의 작은 문서로 검사 규칙을 확인하고, 뒤쪽은 레포에 든 실제 묶음이 통과하는지 본다(회귀 방지)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_alignment as ca  # noqa: E402
import validate_policy_pack as vpp  # noqa: E402

MD = """# 시험 규정

## 제1장 총칙

제1조(목적)
① 이 규정은 시험을 위한 것이다.
② 반품은 받은 날부터 7일 이내에 신청한다.

제2조(환불)
① 환불은 3영업일 이내에 한다.

## 부칙

제1조(시행일) 이 규정은 2026년 11월 3일부터 시행한다.

[별표 1] 배송비
| 구분 | 금액 |
| 일반 택배 | 3,000원 |
"""
SEEDS = {"ws": {3: {"id": 3, "tenant_id": 1, "parent_id": None}}, "tenants": {1: "retail"}, "docs": []}


def _doc(tmp_path, slug="retail-t-2026-11", md=MD, **meta):
    d = tmp_path / slug; d.mkdir()
    (d / f"{slug}.md").write_text(md, encoding="utf-8", newline="\n")
    m = {"slug": slug, "tenant": "retail", "workspace_id": 3, "title": "시험 규정", "version": "2026-11", "doc_type": "규정", "source_type": "markdown",
         "effective_from": "2026-11-03", "tags": ["a", "b", "c"], "terms": ["반품"], "author": "t", "summary": "s"} | meta
    return d, m


def test_clean_document_has_no_errors(tmp_path):
    d, m = _doc(tmp_path)
    E, W = [], []
    body = vpp.check_body(d.name, d / f"{d.name}.md", E, W)
    vpp.check_meta(d.name, d, m, body["text"], SEEDS, E, W)
    assert E == [] and [c["id"] for c in body["chunks"]][:2] == ["retail-t-2026-11-a1-1", "retail-t-2026-11-a2-1"]


def test_body_rule_violations_are_errors(tmp_path):
    d, _ = _doc(tmp_path, md=MD.replace("제2조(환불)", "## 제2조(환불)").replace("| 일반 택배", "|---|---|\n| 일반 택배"))
    E, W = [], []
    vpp.check_body(d.name, d / f"{d.name}.md", E, W)
    assert any("조문 줄에 #" in e for e in E) and any("표 구분선" in e for e in E)


def test_meta_rejects_source_lists_and_wrong_company(tmp_path):
    d, m = _doc(tmp_path, tenant="it", companies=["x"], effective_from="2026-11-01")
    E, W = [], []
    body = vpp.check_body(d.name, d / f"{d.name}.md", E, W)
    vpp.check_meta(d.name, d, m, body["text"], SEEDS, E, W)
    joined = " ".join(E)
    assert "표준 밖 키 ['companies']" in joined and "tenant 'it'" in joined and "부칙 시행일 2026-11-03" in joined


def test_same_title_in_one_workspace_needs_supersede_chain():
    metas = {"a-1": {"workspace_id": 3, "title": "환불규정"}, "a-2": {"workspace_id": 3, "title": "환불규정"}}
    seeds = SEEDS | {"docs": [{"slug": "refund-v3", "workspace_id": 3, "title": "환불규정", "status": "active"}]}
    errs = {"a-1": [], "a-2": []}
    vpp.check_collisions(metas, seeds, errs)
    assert any("서로를 개정으로" in e for e in errs["a-1"]) and any("시드 'refund-v3'" in e for e in errs["a-2"])
    ok = {"a-1": []}
    vpp.check_collisions({"a-1": {"workspace_id": 3, "title": "환불규정", "supersedes": "refund-v3"}}, seeds, ok)
    assert ok["a-1"] == []


def _questions(tmp_path, rows):
    d, _ = _doc(tmp_path)
    q = d / f"{d.name}.questions.md"
    head = "| # | 유형 | 질의 | 정답 조문 | 정답 요지 |\n|---|---|---|---|---|\n"
    q.write_text(head + "\n".join(f"| {i} | {t} | {qq} | {a} | {g} |" for i, (t, qq, a, g) in enumerate(rows, 1)) + "\n", encoding="utf-8")
    _, idx = vpp.article_index(d / f"{d.name}.md")
    E, W = [], []
    vpp.check_questions(d.name, q, idx, (d / f"{d.name}.md").read_text(encoding="utf-8"), ["가짜마켓"], E, W)
    return E


def test_questions_must_be_answerable_from_cited_article(tmp_path):
    E = _questions(tmp_path, [
        ("기본", "반품은 며칠 안에 신청해야 해?", "제1조 ②", "받은 날부터 7일 이내"),            # 통과
        ("기본", "환불은 며칠 안에 해줘?", "제2조 ①", "5영업일 이내"),                          # 숫자가 조문에 없음
        ("기본", "환불 기한이 어떻게 돼?", "제9조 ①", "3영업일 이내"),                          # 없는 조문
        ("기본", "제2조 환불 기한 알려줘", "제2조 ①", "3영업일 이내"),                          # 질의에 조문 번호
        ("표", "택배비는 얼마야?", "별표 1 일반 택배 행", "3,000원"),                           # 통과
        ("표", "퀵 배송비는 얼마야?", "별표 1 퀵 배송 행", "3,000원"),                           # 없는 행
        ("의역", "가짜마켓에서 산 건 며칠 안에 물러?", "제1조 ②", "7일 이내"),                   # 금지 고유명사
        ("기본", "반품 신청 기한의 항이 맞나?", "제1조 ③", "7일 이내"),                          # 없는 항
    ])
    joined = "\n".join(E)
    assert "#1:" not in joined and "#5:" not in joined
    assert "#2: 정답 요지의 숫자 ['5영업일']" in joined and "문서 전체에도 없음" in joined
    assert "#3: 정답 조문 제9조 가 본문에 없음" in joined
    assert "#4: 질의에 조문·별표 번호" in joined
    assert "#6: 별표1 에 '퀵 배송' 행이 없음" in joined
    assert "#7: 금지 고유명사 ['가짜마켓']" in joined
    assert "#8: 제1조 에 ③항이 없음" in joined


def test_repo_policy_pack_passes(capsys, monkeypatch):
    """레포의 data/policies/src 전체가 오류 0 이어야 한다(질의표 포함)."""
    monkeypatch.setattr(sys, "argv", ["validate_policy_pack.py", "--quiet"])
    assert vpp.main() == 0, capsys.readouterr().out


@pytest.mark.parametrize("tenant", sorted(p.stem for p in (ROOT / "data" / "alignment").glob("*.json")))
def test_alignment_tables_hold(tenant):
    """대조표의 값이 규정 조문과 로그 본문 양쪽에 실제로 있어야 한다."""
    errs, stats = ca.check(tenant)
    assert errs == [], "\n".join(errs)
    assert stats["by"]["exception"] >= 1 and stats["by"]["gap"] >= 1   # 예외·공백 사례가 시연에 필요하다


def test_alignment_detects_drift(tmp_path, monkeypatch):
    """규정 쪽 숫자만 바뀌면 걸려야 한다."""
    src = json.loads((ROOT / "data" / "alignment" / "retail.json").read_text(encoding="utf-8"))
    link = next(l for l in src["links"] if l.get("values") and l["relation"] == "follows")
    link["values"] = link["values"] + ["9999초"]
    (tmp_path / "retail.json").write_text(json.dumps({"tenant": "retail", "links": [link]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ca, "ALIGN", tmp_path)
    errs, _ = ca.check("retail")
    assert any("9999초" in e for e in errs)
