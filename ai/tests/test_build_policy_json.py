"""원문 → 청크 JSON 변환기(scripts/build_policy_json.py) 단위 테스트. DB 불필요."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import build_policy_json as b  # noqa: E402
import index_policy  # noqa: E402

SAMPLE = """\
소비자분쟁해결기준 (공정거래위원회 고시)

제1조(목적) 이 기준은 소비자기본법 제16조제2항에 따라 분쟁을 원활하게 해결하기 위한 합의 또는 권고의 기준을 정함을 목적으로 한다.

제2조 (정의) ① 이 기준에서 사용하는 용어의 뜻은 다음과 같다.
② "사업자"란 물품을 제조·수입·판매하는 자를 말한다.
③ "소비자"란 사업자가 제공하는 물품을 소비생활을 위하여 사용하는 자를 말한다.

제2조의2(적용 범위) 이 기준은 품목별 해결기준에서 달리 정하지 않는 한 모든 품목에 적용한다.

## 품목별 해결기준 — 식료품
개봉된 신선식품은 소비자의 귀책사유가 없는 한 구입 후 7일 이내 교환 또는 환급이 가능하다.

[별표 1] 품목별 보상기준
유통기한 경과 제품 판매 시: 제품 교환 또는 구입가 환급.

Q18. VPN 접속이 거부될 때
승인된 기간이 만료되었거나 단말 보안 점검을 통과하지 못한 경우 접속이 거부됩니다.
"""


def args(**kw):
    base = dict(slug="test-doc", workspace=3, title="테스트 기준", version="v1", doc_type="규정", source_type="markdown", file_path=None,
                effective_from=None, supersedes=None, tags=["t"], max_chars=700)
    base.update(kw)
    return SimpleNamespace(**base)


def test_split_articles_recognizes_all_boundary_kinds(tmp_path):
    arts = b.split_articles(SAMPLE.splitlines())
    labels = [a["label"] for a in arts]
    assert labels == ["서문", "제1조 (목적)", "제2조 (정의)", "제2조의2 (적용 범위)", "품목별 해결기준 — 식료품", "[별표 1] 품목별 보상기준", "Q18. VPN 접속이 거부될 때"]
    assert arts[2]["text"].startswith("① 이 기준에서") and "③" in arts[2]["text"]
    assert arts[1]["title"] == "목적"


def test_build_ids_are_deterministic_and_validate(tmp_path):
    src = tmp_path / "src.md"
    src.write_text(SAMPLE, encoding="utf-8")
    doc = b.build(src, args())
    ids = [c["id"] for c in doc["chunks"]]
    assert ids == ["test-doc-s1-1", "test-doc-a1-1", "test-doc-a2-1", "test-doc-a2-2-1", "test-doc-s5-1", "test-doc-s6-1", "test-doc-q18-1"]
    assert len(ids) == len(set(ids)), "청크 id 는 유일"
    assert index_policy.validate(doc, src) == []
    assert doc["meta"]["converter"] == "build_policy_json.py"
    # 같은 입력 → 같은 출력
    assert json.dumps(b.build(src, args()), sort_keys=True) == json.dumps(doc, sort_keys=True)


def test_long_article_splits_on_paragraph_marks_keeping_label(tmp_path):
    long = "제5조(긴 조문) " + " ".join(f"{m} {'가' * 150}." for m in "①②③④⑤")
    src = tmp_path / "long.md"
    src.write_text(long, encoding="utf-8")
    doc = b.build(src, args(max_chars=400))
    labels = {c["article_label"] for c in doc["chunks"]}
    assert labels == {"제5조 (긴 조문)"}
    assert len(doc["chunks"]) >= 2 and all(len(c["text"]) <= 400 for c in doc["chunks"])
    assert [c["id"] for c in doc["chunks"]] == [f"test-doc-a5-{i}" for i in range(1, len(doc["chunks"]) + 1)]
    assert all(c["text"].lstrip()[0] in "①②③④⑤" for c in doc["chunks"]), "항 경계에서 자른다"


def test_table_sections_split_on_row_boundaries(tmp_path):
    rows = [f"| {i}) 분쟁유형 {i} | o 해결기준 {'가' * 60} | 비고 |" for i in range(1, 13)]
    src = tmp_path / "tbl.md"
    src.write_text("## 별표 Ⅱ 상품 › 2. 식료품\n\n| 분쟁유형 | 해결기준 | 비고 |\n" + "\n".join(rows) + "\n", encoding="utf-8")
    doc = b.build(src, args(max_chars=400))
    assert {c["article_label"] for c in doc["chunks"]} == {"별표 Ⅱ 상품 › 2. 식료품"}
    assert len(doc["chunks"]) >= 3 and all(len(c["text"]) <= 400 for c in doc["chunks"])
    for c in doc["chunks"]:
        assert all(l.startswith("|") and l.endswith("|") for l in c["text"].splitlines()), "행이 중간에 잘리지 않는다"
    assert sum(c["text"].count("\n") + 1 for c in doc["chunks"]) == 13, "행 수 보존(머리행 1 + 12)"


def test_sub_headings_extend_parent_label(tmp_path):
    src = tmp_path / "sub.md"
    src.write_text("# 별표 Ⅰ 대상품목\n| 번호 | 업종 |\n\n## 별표 Ⅱ 상품 › 5. 공산품\n공통 규정.\n\n#### ⑤자동차\n| 1) 하자 | o 교환 |\n\n"
                   "## 별표 Ⅱ 서비스업 › 9. 운수 관련\n\n### ①운수업\n| 0) 공통 | o |\n\n#### 항공(국제 여객)\n| 1) 지연 | o 배상 |\n\n"
                   "### ③택배\n| 1) 분실 | o 배상 |\n", encoding="utf-8")
    labels = [c["article_label"] for c in b.build(src, args())["chunks"]]
    assert labels == ["별표 Ⅰ 대상품목", "별표 Ⅱ 상품 › 5. 공산품", "별표 Ⅱ 상품 › 5. 공산품 › ⑤자동차",
                      "별표 Ⅱ 서비스업 › 9. 운수 관련 › ①운수업", "별표 Ⅱ 서비스업 › 9. 운수 관련 › ①운수업 › 항공(국제 여객)",
                      "별표 Ⅱ 서비스업 › 9. 운수 관련 › ③택배"], "###는 ## 뒤에, ####는 ###까지 이어 붙고 새 ###에서 ####는 초기화"


def test_supersedes_and_no_articles(tmp_path):
    src = tmp_path / "x.md"
    src.write_text("제1조(목적) 테스트", encoding="utf-8")
    assert b.build(src, args(supersedes="old-slug"))["supersedes"] == "old-slug"
    src.write_text("   \n\n", encoding="utf-8")
    assert b.build(src, args())["chunks"] == []
