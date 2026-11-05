"""PDF 원문에서 조문 라벨이 있는 쪽을 찾아 chunks.json 의 page(·bboxes) 를 채운다 (D-4 ②, ADR-001 출처 뷰어 "원문 보기").

  - 청크마다 article_label 의 머리("제12조", "부칙 제1조", "별표 1", "Q18")를 공백·괄호 차이를 무시하고 쪽 본문에서 찾는다.
    한 라벨이 여러 쪽에 나오면(목차·본문) 앞 청크의 쪽 이후에서 가장 먼저 나오는 쪽 — 조문은 문서 순서대로라는 가정.
  - page 는 1부터. 못 찾으면 null 로 두고 목록을 찍는다(실패가 아니라 집필자 확인 대상).
  - bboxes 는 라벨 줄 사각형 하나 [[x0, y0, x1, y1]] (PDF 좌표, 1쪽 기준). 못 잡으면 비운다 — 하이라이트는 보너스.
  - Markdown 이 정본이고 PDF 는 출력물이라는 전제(프론트팀 지시서 §A-4). 조문 번호·제목이 같아야 잡힌다.

실행: uv run --extra pdf python scripts/pdf_locate.py data/policies/<slug>.chunks.json [--pdf src/<slug>/<slug>.pdf] [--write]
      (ai/ 에서: uv run --extra pdf python ../scripts/pdf_locate.py ...). pymupdf 는 ai/pyproject.toml 의 extra "pdf".
      --write 가 없으면 결과만 출력. make pdf SLUG=<slug> 가 --write + 서빙 위치(data/files/policies/) 복사까지.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = re.compile(r"\s+")
HEAD_RE = re.compile(r"^(부칙\s*)?(제\s*\d+\s*조(?:의\s*\d+)?|별표\s*\d*|Q\s*\d+)", re.I)


def label_key(label: str) -> str | None:
    """'제12조 (원격접속 신청)' → '제12조', '부칙 제1조 (시행일)' → '부칙제1조', '[별표 1] 품목별' → '별표1', 'Q18. VPN' → 'q18'. 없으면 None."""
    m = HEAD_RE.match(label.replace("[", "").replace("]", "").strip())
    if not m:
        return None
    return WS.sub("", (m.group(1) or "") + m.group(2)).lower()


def norm(text: str) -> str:
    return WS.sub("", text.replace("（", "(").replace("）", ")").replace("[", "").replace("]", "")).lower()


def full_key(label: str) -> str:
    """라벨 전체를 공백·괄호 종류 차이 없이: '제1조 (목적)' → '제1조(목적)'. 목차 줄('제1조 목적 ... 1')과 구분하는 데 쓴다."""
    return norm(label)


def page_texts(pdf_path: Path) -> list[str]:
    import pymupdf  # ai extra "pdf"
    with pymupdf.open(pdf_path) as doc:
        return [p.get_text("text") for p in doc]


def find_bbox(pdf_path: Path, page_no: int, label: str) -> list[list[float]]:
    """라벨 머리(예: '제12조')가 있는 줄의 사각형. 그 쪽에서 첫 번째 일치만."""
    import pymupdf
    key = HEAD_RE.match(label.replace("[", "").replace("]", "").strip())
    needle = WS.sub(" ", (key.group(0) if key else label)).strip()
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_no - 1]
        for cand in (needle, needle.replace(" ", "")):
            hits = page.search_for(cand)
            if hits:
                r = hits[0]
                return [[round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]]
    return []


def _first_page(lines: list[list[str]], key: str, start: int, at_line_start: bool) -> int | None:
    order = list(range(start, len(lines))) + list(range(0, start))      # 뒤에 없으면 앞도 본다(부칙·별표가 앞에 오는 PDF)
    for i in order:
        if any(l.startswith(key) if at_line_start else key in l for l in lines[i]):
            return i
    return None


def locate(doc: dict, texts: list[str], pdf_path: Path | None = None, want_bboxes: bool = True) -> dict:
    """chunks 의 page(·bboxes) 를 채운다. 반환: {found, missing: [label...], pages}. 같은 라벨의 분할 청크는 같은 쪽.

    찾는 순서: ① 줄 머리가 라벨 전체('제1조(목적)')로 시작하는 쪽 — 목차 줄('제1조 목적 … 1')은 제외됨 ② 줄 머리가 라벨 키('제1조')로 시작
    ③ 본문 어디든 키가 있는 쪽(참조 문구라도). '부칙 제1조' 는 '부칙' 줄이 있는 쪽부터 찾는다(PDF 에서 부칙과 조문이 다른 줄).
    """
    lines = [[norm(l) for l in t.splitlines() if l.strip()] for t in texts]
    found, missing, cache = 0, [], {}
    cursor = 0                                    # 조문은 문서 순서 — 이전 라벨의 쪽부터 찾는다 (목차 쪽을 건너뜀)
    for c in doc["chunks"]:
        label = c["article_label"]
        if label in cache:
            c["page"], c["bboxes"] = cache[label]
            found += c["page"] is not None
            continue
        key = label_key(label)
        full = full_key(label)
        idx = None
        if key and key.startswith("부칙"):
            p0 = _first_page(lines, "부칙", cursor, True)
            if p0 is not None:
                sub_full, sub_key = full[len("부칙"):], key[len("부칙"):]
                idx = _first_page(lines, sub_full, p0, True)
                if idx is None:
                    idx = _first_page(lines, sub_key, p0, True)
        elif key:
            for needle, at_start in ((full, True), (key, True), (key, False)):
                idx = _first_page(lines, needle, cursor, at_start)
                if idx is not None:
                    break
        page_no = idx + 1 if idx is not None else None
        if idx is not None:
            cursor = idx
        bboxes = find_bbox(pdf_path, page_no, label) if (page_no and pdf_path and want_bboxes) else []
        c["page"] = page_no
        c["bboxes"] = bboxes or None
        cache[label] = (page_no, bboxes or None)
        if page_no is None:
            missing.append(label)
        else:
            found += 1
    return {"found": found, "missing": missing, "pages": len(texts)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chunks", type=Path, help="data/policies/<slug>.chunks.json")
    ap.add_argument("--pdf", type=Path, default=None, help="기본 data/policies/src/<slug>/<slug>.pdf")
    ap.add_argument("--write", action="store_true", help="chunks.json 에 page·bboxes 를 써 넣는다")
    ap.add_argument("--no-bboxes", action="store_true")
    a = ap.parse_args()
    doc = json.loads(a.chunks.read_text(encoding="utf-8"))
    pdf = a.pdf or (ROOT / "data" / "policies" / "src" / doc["slug"] / f"{doc['slug']}.pdf")
    if not pdf.exists():
        print(f"PDF 없음: {pdf}"); return 1
    try:
        texts = page_texts(pdf)
    except ImportError:
        print("pymupdf 가 없습니다: cd ai && uv sync --extra pdf  (또는 uv run --with pymupdf)"); return 1
    r = locate(doc, texts, pdf, want_bboxes=not a.no_bboxes)
    total = len(doc["chunks"])
    print(f"{doc['slug']}: {r['pages']}쪽, 청크 {total}개 중 page 채움 {r['found']}개" + (f", 못 찾음 {len(r['missing'])}개" if r["missing"] else ""))
    for m in r["missing"]:
        print(f"  ✗ {m}")
    if a.write:
        a.chunks.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {a.chunks.relative_to(ROOT) if a.chunks.is_relative_to(ROOT) else a.chunks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
