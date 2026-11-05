"""PDF 쪽 번호 추출(scripts/pdf_locate.py) 단위 테스트 — pymupdf(extra "pdf")가 있을 때만. DB 불필요."""
import sys
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="pymupdf 없음: uv sync --extra pdf")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import pdf_locate as pl  # noqa: E402


def _pdf(path: Path, pages: list[list[str]]) -> Path:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11, fontname="helv" if line.isascii() else "korea")
            y += 18
    doc.save(path)
    doc.close()
    return path


def test_locate_fills_page_and_bbox_and_lists_missing(tmp_path):
    """목차 쪽의 '제12조' 는 건너뛰고 조문 제목 줄이 있는 쪽을 잡는다. 분할 청크는 같은 쪽. 부칙은 '부칙' 줄 뒤의 '제1조'. 없는 라벨은 null + 목록."""
    pdf = _pdf(tmp_path / "vpn-guide-v30.pdf", [
        ["VPN·원격접속 운영지침 v3.0", "목차", "제1조 목적 ... 1", "제12조 원격접속 신청 ... 2", "부칙 ... 3"],
        ["제1조(목적) 이 지침은 원격접속의 기준을 정한다.", "제12조(원격접속 신청) ① 사외 접속은 사전 신청한다.", "② 부서장 승인을 받는다."],
        ["제14조(접속 차단) 승인 만료 시 차단한다.", "부칙", "제1조(시행일) 이 지침은 2026년 11월 1일부터 시행한다."],
    ])
    doc = {"slug": "vpn-guide-v30", "chunks": [
        {"id": "vpn-guide-v30-a1-1", "article_label": "제1조 (목적)", "page": None, "text": "…"},
        {"id": "vpn-guide-v30-a12-1", "article_label": "제12조 (원격접속 신청)", "page": None, "text": "① …"},
        {"id": "vpn-guide-v30-a12-2", "article_label": "제12조 (원격접속 신청)", "page": None, "text": "② …"},
        {"id": "vpn-guide-v30-a14-1", "article_label": "제14조 (접속 차단)", "page": None, "text": "…"},
        {"id": "vpn-guide-v30-p1-1", "article_label": "부칙 제1조 (시행일)", "page": None, "text": "…"},
        {"id": "vpn-guide-v30-a99-1", "article_label": "제99조 (없는 조문)", "page": None, "text": "…"},
    ]}
    r = pl.locate(doc, pl.page_texts(pdf), pdf)
    pages = [c["page"] for c in doc["chunks"]]
    assert pages == [2, 2, 2, 3, 3, None]
    assert r["found"] == 5 and r["missing"] == ["제99조 (없는 조문)"] and r["pages"] == 3
    assert doc["chunks"][1]["bboxes"] and len(doc["chunks"][1]["bboxes"][0]) == 4     # 라벨 줄 사각형 하나
    assert doc["chunks"][5]["bboxes"] is None


def test_label_key_variants():
    assert pl.label_key("제12조 (원격접속 신청)") == "제12조"
    assert pl.label_key("제2조의2(적용 범위)") == "제2조의2"
    assert pl.label_key("부칙 제1조 (시행일)") == "부칙제1조"
    assert pl.label_key("[별표 1] 품목별 환불 기간") == "별표1"
    assert pl.label_key("Q18. VPN 접속이 거부될 때") == "q18"
    assert pl.label_key("품목별 해결기준 — 식료품") is None
