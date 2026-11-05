"""questions.md → evalset 변환(scripts/questions_to_evalset.py) 단위 테스트. DB 불필요."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import questions_to_evalset as qe  # noqa: E402

SLUG = "t-v1"
CHUNKS = [
    {"id": f"{SLUG}-a5-1", "article_label": "제5조 (기간)", "text": "① 14일. ② 3개월."},
    {"id": f"{SLUG}-a9-1", "article_label": "제9조 (긴 조문)", "text": "① 앞부분. ② 중간."},
    {"id": f"{SLUG}-a9-2", "article_label": "제9조 (긴 조문)", "text": "③ 뒷부분. ④ 끝."},
    {"id": f"{SLUG}-a17-2-1", "article_label": "제17조의2 (예외)", "text": "① 예외."},
    {"id": f"{SLUG}-s30-1", "article_label": "[별표 2] 비용", "text": "| 구분 | 비용 |\n| 일반 택배 | 3,000원 |"},
    {"id": f"{SLUG}-s30-2", "article_label": "[별표 2] 비용", "text": "| 설치 후 대형 가전·가구 | 30,000원 |"},
]


def test_article_hang_filters_split_chunks():
    assert qe.map_refs("제5조 ①", CHUNKS, SLUG) == [f"{SLUG}-a5-1"]            # 분할 없음 → 그 청크
    assert qe.map_refs("제9조 ③④", CHUNKS, SLUG) == [f"{SLUG}-a9-2"]          # 분할 조문은 항이 있는 청크만
    assert qe.map_refs("제9조", CHUNKS, SLUG) == [f"{SLUG}-a9-1", f"{SLUG}-a9-2"]  # 항 없음 → 전부
    assert qe.map_refs("제17조의2 ①", CHUNKS, SLUG) == [f"{SLUG}-a17-2-1"]     # 조의N 은 a17-2, a17 과 섞이지 않음
    assert qe.map_refs("제17조 ①", CHUNKS, SLUG) == []


def test_table_row_by_keyword():
    assert qe.map_refs("별표 2 설치 후 대형 가전·가구 행", CHUNKS, SLUG) == [f"{SLUG}-s30-2"]
    assert qe.map_refs("별표 2 택배 행", CHUNKS, SLUG) == [f"{SLUG}-s30-1"]
    assert qe.map_refs("별표 2 없는 행", CHUNKS, SLUG) == []                      # 2청크 별표에서 행 못 찾음 → 실패
    assert qe.map_refs("별표 3 없는 행", CHUNKS, SLUG) == []                      # 별표 자체가 없음


def test_cross_doc_ref_is_split_off():
    own, other = qe.split_refs("제4조 ④ (+ 고객응대 근로자 보호 지침 제11조 ①)")
    assert (own, other) == ("제4조 ④", "고객응대 근로자 보호 지침 제11조 ①")
    assert qe.split_refs("별표 3 환급 행") == ("별표 3 환급 행", "")


def test_cross_doc_ref_maps_to_the_other_documents_chunks_within_scope():
    docs = [{"title": "급여 규정", "slug": "pay", "ws": 2, "chunks": [{"id": "pay-a18-1", "article_label": "제18조", "text": "① 유급."}]},
            {"title": "개인정보 보호 내부관리계획", "slug": "plan", "ws": 10,
             "chunks": [{"id": "plan-a18-1", "article_label": "제18조", "text": "x"}, {"id": "plan-a19-1", "article_label": "제19조", "text": "y"}]}]
    ids, outside = qe.map_cross("급여 규정 제18조 ①, 개인정보 보호 내부관리계획 제18조·제19조", docs, {3, 10})
    assert ids == ["plan-a18-1", "plan-a19-1"], "그 부서에서 검색되는 문서만 정답으로 삼는다"
    assert outside == ["급여 규정"], "다른 부서의 규정은 검색 범위 밖"


def test_parse_rows_reads_table():
    md = "# t\n\n| # | 유형 | 질의 | 정답 조문 | 정답 요지 |\n|---|---|---|---|---|\n| 1 | 기본 | 며칠? | 제5조 ① | 14일 |\n"
    rows = qe.parse_rows(md)
    assert rows == [{"no": 1, "type": "기본", "query": "며칠?", "answer": "제5조 ①", "gist": "14일"}]


def test_faq_label_and_spaced_table_keyword():
    chunks = CHUNKS + [{"id": f"{SLUG}-q18-1", "article_label": "Q18. 승인 기간", "text": "만료 시 차단"},
                       {"id": f"{SLUG}-s31-1", "article_label": "[별표 3] 금품", "text": "| 경조 금품 | 5만원 |"}]
    assert qe.map_refs("Q18", chunks, SLUG) == [f"{SLUG}-q18-1"]
    assert qe.map_refs("별표 3 경조금 행", chunks, SLUG) == [f"{SLUG}-s31-1"]   # 공백 무시
    assert qe.map_refs("별표 3 엉뚱한 행", chunks, SLUG) == [f"{SLUG}-s31-1"]   # 한 청크짜리 별표는 행이 안 맞아도 그 청크
