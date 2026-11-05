"""규정 원문 <slug>.md → <slug>.pdf (출처 뷰어의 '원문 보기' 용 텍스트 PDF).

Markdown 이 정본이고 PDF 는 그 출력물이다(§A-4). 본문을 고치면 이 스크립트로 PDF 를 다시 만든다 — 손으로 만든 PDF 는 본문과 어긋나기 쉽다.
조문 제목 줄('제12조(원격접속 신청)')과 별표 줄('[별표 1] …')은 Markdown 과 글자 단위로 같은 한 줄로 찍는다 — pdf_locate.py 가 그 줄로 쪽 번호를 찾는다.
글꼴은 PyMuPDF 내장 한글 글꼴(Droid Sans Fallback, Apache 2.0)이라 외부 글꼴 파일이 필요 없다. 같은 원문이면 같은 PDF 가 나온다(메타 날짜 고정).

실행: cd ai && uv run --with "pymupdf>=1.25,<2" --with fonttools python ../scripts/md_to_pdf.py <slug> [<slug> ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "policies" / "src"
W, H, MARGIN = 595, 842, 56          # A4 세로(pt)
BODY, LEAD = 10.5, 16.5
ARTICLE = re.compile(r"^제\s*\d+\s*조")


def wrap(font: pymupdf.Font, text: str, size: float, width: float) -> list[str]:
    """글자 폭을 재서 줄을 나눈다. 공백에서 끊는 것을 우선하고, 없으면 글자 단위로 끊는다(한글은 어디서 끊어도 된다)."""
    out, line = [], ""
    for word in re.split(r"(\s+)", text):
        if font.text_length(line + word, size) <= width:
            line += word
            continue
        if line.strip():
            out.append(line.rstrip()); line = ""
        word = word.lstrip() if not line else word
        while font.text_length(word, size) > width:
            cut = len(word)
            while cut > 1 and font.text_length(word[:cut], size) > width:
                cut -= 1
            out.append(word[:cut]); word = word[cut:]
        line = word
    if line.strip():
        out.append(line.rstrip())
    return out or [""]


def render(slug: str) -> Path:
    md = (SRC / slug / f"{slug}.md").read_text(encoding="utf-8").splitlines()
    font = pymupdf.Font("korea")
    doc = pymupdf.open()
    state = {"page": None, "tw": None, "y": 0.0}

    def new_page():
        if state["tw"] is not None:
            state["tw"].write_text(state["page"])
        state["page"] = doc.new_page(width=W, height=H)
        state["tw"] = pymupdf.TextWriter(state["page"].rect)
        state["y"] = MARGIN

    def put(text: str, size: float = BODY, before: float = 0.0, indent: float = 0.0, keep: int = 1):
        lines = wrap(font, text, size, W - 2 * MARGIN - indent)
        lead = LEAD * size / BODY
        if state["y"] + before + lead * min(len(lines), keep + 1) > H - MARGIN:   # 제목 줄이 쪽 끝에 홀로 남지 않게
            new_page(); before = 0.0
        state["y"] += before
        for ln in lines:
            if state["y"] + lead > H - MARGIN:
                new_page()
            state["y"] += lead
            state["tw"].append((MARGIN + indent, state["y"]), ln, font=font, fontsize=size)

    new_page()
    for raw in md:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            put(line[2:].strip(), size=17, before=6); state["y"] += 10
        elif line.startswith("## "):
            put(line[3:].strip(), size=13, before=16, keep=2); state["y"] += 2
        elif ARTICLE.match(line):
            put(line.strip(), before=9, keep=2)
        elif line.lstrip().startswith("[별표"):
            put(line.strip(), size=11.5, before=14, keep=2)
        elif line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            put("  |  ".join(cells), size=9.5, indent=8)
        elif re.match(r"^\s*\d+\.\s", line) or re.match(r"^\s*[가-하]\.\s", line):
            put(line.strip(), indent=14)
        else:
            put(line.strip())
    state["tw"].write_text(state["page"])
    for i, page in enumerate(doc, 1):     # 쪽 번호
        tw = pymupdf.TextWriter(page.rect)
        label = f"- {i} -"
        tw.append(((W - font.text_length(label, 9)) / 2, H - 28), label, font=font, fontsize=9)
        tw.write_text(page)
    title = md[0][2:].strip() if md and md[0].startswith("# ") else slug
    doc.set_metadata({"title": title, "author": "", "subject": slug, "creator": "md_to_pdf.py", "producer": "PyMuPDF",
                      "creationDate": "D:20261103000000+09'00'", "modDate": "D:20261103000000+09'00'"})
    doc.subset_fonts()                    # 쓰인 글자만 포함 (fonttools 필요) — 없으면 1.6 MB, 있으면 수십 KB
    dest = SRC / slug / f"{slug}.pdf"
    doc.save(dest, garbage=4, deflate=True, no_new_id=True)
    return dest


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    slugs = sys.argv[1:]
    if not slugs:
        print(__doc__); return 1
    for slug in slugs:
        if not (SRC / slug / f"{slug}.md").exists():
            print(f"원문 없음: {slug}"); return 1
        dest = render(slug)
        with pymupdf.open(dest) as d:
            text = "".join(p.get_text() for p in d)
            labels = [l.strip() for l in (SRC / slug / f"{slug}.md").read_text(encoding="utf-8").splitlines() if ARTICLE.match(l) or l.startswith("[별표")]
            flat = re.sub(r"\s+", "", text)
            lost = [l for l in labels if re.sub(r"\s+", "", l.split(")")[0]) not in flat]
            print(f"wrote {dest.relative_to(ROOT)}: {len(d)}쪽, {dest.stat().st_size // 1024} KB, 조문·별표 줄 {len(labels)}개 중 누락 {len(lost)}")
            if lost:
                print("  누락: " + ", ".join(lost[:5])); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
