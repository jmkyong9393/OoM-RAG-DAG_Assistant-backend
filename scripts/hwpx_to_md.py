"""HWPX(한글 문서, zip+XML) → Markdown 변환기. 규정 별표처럼 표 위주인 문서를 build_policy_json.py 가 읽을 수 있는 텍스트로 만든다.

출력 규칙
  - 문서 첫 줄이 '<별표 Ⅱ>' 이면 다음 줄 제목과 합쳐 '# 별표 Ⅱ 품목별 해결기준' 으로
  - 표 밖의 'Ⅰ. 상품(재화)' 줄은 부(部) 이름으로 기억하고, '1. 농ㆍ수ㆍ축산물(7개 업종)' 줄은
    '## 별표 Ⅱ 상품 › 1. 농ㆍ수ㆍ축산물(7개 업종)' 제목으로 → 업종 하나가 청크 하나의 경계가 된다
  - 표는 행마다 '| 셀 | 셀 | 셀 |' 한 줄. 세로 병합 셀은 짧은 값(업종명·번호)만 아래 행에 채운다
  - '< ①운수업(9개 업종) >' 문단은 '### ①운수업', 표 첫 행이 가로 병합된 '⑤자 동 차 (2 - 1)' 은 '#### ⑤자동차' 소제목으로
    (build_policy_json.py 가 상위 ## label 뒤에 ' › ' 로 이어 붙인다)
  - '분      쟁      유      형' 처럼 자간을 띄운 제목은 '분쟁유형' 으로 모은다

실행: uv run scripts/hwpx_to_md.py 별표1.hwpx 별표2.hwpx … --out 원문.md [--prelude 머리말.md]
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
PART_RE = re.compile(rf"^([{ROMAN}]+)\s*\.\s*(.+)$")
ITEM_RE = re.compile(r"^(\d{1,3})\s*\.\s*(\S.*)$")
BYL_RE = re.compile(rf"^[<＜〈]\s*별\s*표\s*([{ROMAN}\d]+)\s*[>＞〉]$")
SPACED_RE = re.compile(r"^(\S\s+){1,}\S$")  # '분  쟁  유  형', '비   고'
FILL_DOWN_MAX = 20  # 세로 병합 셀을 아래 행에 반복할 최대 글자 수. 더 길면 "〃" 로 표시
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚"
SUB_RE = re.compile(r"^[<＜〈]\s*(.+?)\s*[>＞〉]$")                      # '< ①운수업(9개 업종) >' 문단 → ### 소제목
SUBROW_RE = re.compile(rf"^(?:[{CIRCLED}]|\(\d{{1,2}}\))")               # '⑤자 동 차 (2 - 1)', '(21)가 발' 병합 행 → #### 소제목
CONT_RE = re.compile(r"\s*\(\s*\d+\s*[-.]+\s*\d+\s*\)\s*$")               # 표가 이어질 때 붙는 '(2 - 1)', '(3.-2)'
COUNT_RE = re.compile(r"\s*\(\d+\s*개\s*(?:업종|품목|품종)\)")
SUB_MAX = 40


def clean(s: str) -> str:
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s).strip()
    if SPACED_RE.match(s):  # '품  목  별' → '품목별'
        s = re.sub(r"\s+", "", s)
    return s


def sub_label(text: str) -> str:
    """소제목 정리: 이어짐 표시 '(2 - 1)'·'(N개 업종)' 제거, '⑤자 동 차' 처럼 띄운 이름은 모은다."""
    t = COUNT_RE.sub("", CONT_RE.sub("", text)).strip()
    m = re.match(rf"^([{CIRCLED}]|\(\d{{1,2}}\))\s*(.*)$", t)
    head, body = (m.group(1), m.group(2)) if m else ("", t)
    out, run = [], []            # 연속된 한 글자 토큰('신 유 형 상 품 권')은 하나로 모은다
    for tok in body.split() + [""]:
        if len(tok) == 1:
            run.append(tok); continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0]); run = []
        if tok:
            out.append(tok)
    body = re.sub(r"^([가-힣]) (?=[가-힣])", r"\1", " ".join(out))   # '항 공(국내 여객)' → '항공(국내 여객)'
    return head + body


def is_sub_row(cells: list[str]) -> bool:
    """표 첫 행이 가로로 전부 병합된 짧은 제목 셀인가 ('⑤자 동 차 (2 - 1)', '숙박업 (2-1)')."""
    head = cells[0]
    return bool(head) and not any(cells[1:]) and len(head) <= SUB_MAX and bool(SUBROW_RE.match(head) or CONT_RE.search(head))


def para_text(p: ET.Element) -> str:
    """문단 하나의 텍스트. 줄바꿈·탭은 공백으로."""
    parts = []
    for t in p.iter(HP + "t"):
        parts.append("".join(t.itertext()))
    return clean(" ".join(parts))


def cell_text(tc: ET.Element) -> str:
    return clean(" ".join(filter(None, (para_text(p) for p in tc.iter(HP + "p")))))


def table_rows(tbl: ET.Element) -> list[list[str]]:
    rows = int(tbl.get("rowCnt", 0)); cols = int(tbl.get("colCnt", 0))
    grid = [[None] * cols for _ in range(rows)]
    for tc in tbl.iter(HP + "tc"):
        addr = tc.find(HP + "cellAddr"); span = tc.find(HP + "cellSpan")
        if addr is None:
            continue
        r, c = int(addr.get("rowAddr")), int(addr.get("colAddr"))
        rs = int(span.get("rowSpan", 1)) if span is not None else 1
        text = cell_text(tc)
        if r < rows and c < cols:
            grid[r][c] = text
        if rs > 1 and text:
            fill = text if len(text) <= FILL_DOWN_MAX else "〃"
            for rr in range(r + 1, min(r + rs, rows)):
                if grid[rr][c] is None:
                    grid[rr][c] = fill
    out = []
    for row in grid:
        cells = [("" if v is None else v) for v in row]
        if any(cells):
            out.append(cells)
    return out


def convert(path: Path) -> list[str]:
    """HWPX 한 파일 → Markdown 줄 목록."""
    z = zipfile.ZipFile(path)
    sections = sorted((n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n)),
                      key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    lines: list[str] = []
    byl: str | None = None       # '별표 Ⅱ'
    part: str | None = None      # '상품'
    pending_title = False
    next_item = 1                # 업종 번호는 1부터 순서대로. 어긋나면 본문 속 소항목(세탁업 배상기준 1.~8. 등)
    sub_next = 0                 # 진행 중인 소항목 목록의 다음 번호(0이면 없음)
    last_sub = {3: None, 4: None}  # 직전에 낸 ### / #### 소제목 (같은 제목이 이어지면 다시 내지 않음)

    def sub_heading(level: int, label: str):
        if label and label != last_sub[level]:
            lines.extend(["", "#" * level + " " + label, ""])
            last_sub[level] = label
            if level == 3:
                last_sub[4] = None

    for name in sections:
        root = ET.fromstring(z.read(name))
        for p in root.findall(HP + "p"):
            tbls = list(p.iter(HP + "tbl"))
            if tbls:
                for tbl in tbls:
                    rows = table_rows(tbl)
                    if rows and is_sub_row(rows[0]):     # '| ⑤자 동 차 (2 - 1) |  |  |' → #### ⑤자동차
                        sub_heading(4, sub_label(rows[0][0]))
                        rows = rows[1:]
                    for cells in rows:
                        lines.append("| " + " | ".join(cells) + " |")
                    lines.append("")
                continue
            text = para_text(p)
            if not text:
                continue
            m = BYL_RE.match(text)
            if m:
                byl = f"별표 {m.group(1)}"; pending_title = True
                continue
            if pending_title:
                lines += [f"# {byl} {text}", ""]; pending_title = False
                continue
            m = PART_RE.match(text)
            if m:
                part = re.sub(r"\(.*?\)|부문", "", m.group(2)).strip()  # 'Ⅱ. 서비스업 부문' → '서비스업'
                next_item, sub_next = 1, 0
                continue
            m = ITEM_RE.match(text)
            if m:
                n = int(m.group(1))
                if n == sub_next or n == 1 and n != next_item:   # 본문 속 소항목(1., 2., …)이 이어지는 중
                    sub_next = n + 1
                elif n == next_item:
                    prefix = " ".join(filter(None, [byl, part]))
                    lines += ["", f"## {prefix + ' › ' if prefix else ''}{n}. {m.group(2)}", ""]
                    next_item, sub_next = n + 1, 0
                    last_sub[3] = last_sub[4] = None
                    continue
            m = SUB_RE.match(text)
            if m and len(m.group(1)) <= SUB_MAX:                 # '< ①운수업(9개 업종) >' → ### ①운수업
                sub_heading(3, sub_label(m.group(1)))
                continue
            lines.append(text)
    return lines


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--prelude", type=Path, default=None, help="맨 앞에 붙일 Markdown(제목·본문 조문 등)")
    a = ap.parse_args()
    out: list[str] = []
    if a.prelude:
        out += a.prelude.read_text(encoding="utf-8").splitlines() + [""]
    for f in a.files:
        out += convert(f) + [""]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text, encoding="utf-8")
    heads = [l for l in text.splitlines() if l.startswith("#")]
    print(f"wrote {a.out}: {len(text)}자, 제목 {len(heads)}개, 표 행 {sum(l.startswith('|') for l in text.splitlines())}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
