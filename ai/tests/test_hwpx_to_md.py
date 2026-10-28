"""HWPX → Markdown 변환기(scripts/hwpx_to_md.py) 단위 테스트. 작은 HWPX 를 메모리에서 만들어 검증한다. DB 불필요."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import hwpx_to_md as h  # noqa: E402

NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def p(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def tc(col: int, row: int, text: str, colspan: int = 1, rowspan: int = 1) -> str:
    return (f'<hp:tc><hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/>'
            f"<hp:subList>{p(text)}</hp:subList></hp:tc>")


def tbl(rows: int, cols: int, cells: str) -> str:
    return f'<hp:p><hp:run><hp:tbl rowCnt="{rows}" colCnt="{cols}">{cells}</hp:tbl></hp:run></hp:p>'


def make_hwpx(path: Path, sections: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        for i, body in enumerate(sections):
            z.writestr(f"Contents/section{i}.xml", f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="x" {NS}>{body}</hs:sec>')
    return path


SECTION = (
    p("&lt;별표 Ⅱ&gt;") + p("품  목  별    해  결  기  준") + p("Ⅰ. 상품(재화)") + p("1. 농ㆍ수ㆍ축산물(7개 업종)")
    + tbl(4, 3,
          tc(0, 0, "①란류, ②육류", colspan=3)
          + tc(0, 1, "분      쟁      유      형") + tc(1, 1, "해  결  기  준") + tc(2, 1, "비       고")
          + tc(0, 2, "1) 부패, 변질") + tc(1, 2, "o 당해품목 교환 또는 구입가 환급") + tc(2, 2, "* 일실소득은 입증된 때에 한함. " * 3, rowspan=2)
          + tc(0, 3, "2) 이물혼입") + tc(1, 3, "o 당해품목 교환 또는 구입가 환급"))
    + p("Ⅱ. 서비스업 부문") + p("1. 전기ㆍ가스 관련") + p("1. 배상액의 산정방식") + p("2. 손해배상액의 감액") + p("2. 온라인서비스 관련")
)


def test_headings_tables_and_merged_cells(tmp_path):
    lines = h.convert(make_hwpx(tmp_path / "t.hwpx", [SECTION]))
    heads = [l for l in lines if l.startswith("#")]
    assert heads == ["# 별표 Ⅱ 품목별해결기준", "## 별표 Ⅱ 상품 › 1. 농ㆍ수ㆍ축산물(7개 업종)",
                     "## 별표 Ⅱ 서비스업 › 1. 전기ㆍ가스 관련", "## 별표 Ⅱ 서비스업 › 2. 온라인서비스 관련"]
    # 번호가 이어지지 않는 '1. 배상액의 산정방식' 은 본문 속 소항목 → 제목이 아니라 일반 줄
    assert "1. 배상액의 산정방식" in lines and "## 1. 배상액의 산정방식" not in lines
    rows = [l for l in lines if l.startswith("|")]
    assert rows[0] == "| ①란류, ②육류 |  |  |"
    assert rows[1] == "| 분쟁유형 | 해결기준 | 비고 |", "자간을 띄운 제목은 모은다"
    assert rows[2].startswith("| 1) 부패, 변질 | o 당해품목 교환 또는 구입가 환급 | * 일실소득")
    assert rows[3] == "| 2) 이물혼입 | o 당해품목 교환 또는 구입가 환급 | 〃 |", "긴 세로 병합 셀은 〃 로"


def test_short_merged_cell_is_filled_down_and_sections_in_order(tmp_path):
    s0 = p("&lt;별표 Ⅰ&gt;") + p("대상품목") + tbl(3, 2, tc(0, 0, "번호") + tc(1, 0, "품종") + tc(0, 1, "1", rowspan=2) + tc(1, 1, "란류") + tc(1, 2, "육류"))
    s1 = p("※ 비고 문단")
    lines = h.convert(make_hwpx(tmp_path / "u.hwpx", [s0, s1]))
    assert lines[:2] == ["# 별표 Ⅰ 대상품목", ""]
    assert [l for l in lines if l.startswith("|")] == ["| 번호 | 품종 |", "| 1 | 란류 |", "| 1 | 육류 |"]
    assert lines[-1] == "※ 비고 문단", "section1 은 section0 뒤에"


def test_clean_collapses_spacing_only_for_spaced_titles():
    assert h.clean("품  목  별    해  결  기  준") == "품목별해결기준"
    assert h.clean("비       고") == "비고"
    assert h.clean("o 제품교환  또는 구입가 환급") == "o 제품교환 또는 구입가 환급"
