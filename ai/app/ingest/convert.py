"""규정 원본 → 텍스트(조문 줄이 살아 있는 MD). 형식별 추출 + 공통 후처리 + 조문 미리보기. LLM 을 부르지 않는다.

형식: pdf(쪽마다 글자층이 있으면 추출, 없으면 Tesseract OCR) · docx(pandoc) · hwp(HWP 5 OLE 본문 직접) · hwpx(기존 hwpx_to_md) · md · txt.
조문 경계는 기존 분할기(build_policy_json.split_articles)가 정한다 — 여기서는 "제N조" 줄이 줄 머리에 남도록만 정리한다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

from app.config import scripts_dir

OCR_MIN_CHARS = 30      # 쪽의 글자층이 이보다 짧고 그림이 있으면 스캔으로 보고 OCR
OCR_DPI = 300           # 300dpi 조문 경계 100% · 본문 일치 0.92(실측), 200dpi 는 별표 번호를 잘못 읽었다


class ConversionError(Exception):
    """변환할 수 없음(도구 없음 · 깨진 파일). 메시지는 업로드의 error 로 그대로 보인다."""


def _scripts():
    d = str(scripts_dir())
    if d not in sys.path:
        sys.path.insert(0, d)


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ConversionError(f"{tool} 이(가) 설치돼 있지 않습니다")
    return path


# ── 형식별 추출 ─────────────────────────────────────────────────

def _ocr(png: bytes) -> str:
    r = subprocess.run([_need("tesseract"), "stdin", "stdout", "-l", "kor+eng", "--psm", "6"], input=png, capture_output=True, timeout=180)
    if r.returncode != 0:
        raise ConversionError("OCR 실패: " + r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout.decode("utf-8", "replace")


def pdf_text(path: Path) -> tuple[str, dict]:
    import pymupdf as fitz
    pages, how = [], []
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"PDF 를 열 수 없습니다: {e}") from e
    for p in doc:
        t = p.get_text("text")
        if len(t.strip()) >= OCR_MIN_CHARS or not p.get_images():   # 글자가 짧아도 그림이 없으면 원래 짧은 쪽(부칙만 있는 끝 쪽 등)
            pages.append(t); how.append("text")
        else:
            pages.append(ocr_page(p)); how.append("ocr")
    # page_texts 는 보고(DB)가 아니라 원본 옆 pages.json 으로 간다(jobs) — 등록 때 쪽 찾기에서 OCR 을 다시 하지 않게
    return "\n".join(pages), {"pages": len(how), "ocr_pages": [i + 1 for i, h in enumerate(how) if h == "ocr"], "page_texts": pages}


def ocr_page(page) -> str:
    import pymupdf as fitz
    return _ocr(page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY).tobytes("png"))


def pdf_page_texts(path: Path, ocr_pages: list[int]) -> list[str]:
    """쪽 번호 찾기용 쪽별 글자. 글자층이 없어 OCR 로 읽었던 쪽은 다시 OCR 한다(변환 때 쪽 경계를 보관하지 않으므로)."""
    import pymupdf as fitz
    with fitz.open(path) as doc:
        return [ocr_page(p) if i + 1 in ocr_pages else p.get_text("text") for i, p in enumerate(doc)]


def docx_text(path: Path) -> tuple[str, dict]:
    r = subprocess.run([_need("pandoc"), str(path), "-t", "gfm", "--wrap=none"], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise ConversionError("Word 변환 실패: " + r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout.decode("utf-8"), {}


def para_text(raw: bytes) -> str:
    """HWP 문단 글자(HWPTAG_PARA_TEXT, UTF-16LE). 인라인 · 확장 제어(1~9, 11~12, 14~23)는 8 글자 묶음이라 통째로 건너뛴다 — 안 그러면 제어 id 가 한자처럼 섞인다."""
    out, i, n = [], 0, len(raw) // 2
    while i < n:
        c = int.from_bytes(raw[2 * i:2 * i + 2], "little")
        if c >= 32:
            out.append(chr(c)); i += 1
        elif c in (10, 13):
            out.append("\n"); i += 1
        elif c == 9:
            out.append("\t"); i += 8
        elif c == 0 or c >= 24:
            i += 1
        else:
            i += 8
    return "".join(out)


def hwp_text(path: Path) -> tuple[str, dict]:
    """옛 한글(HWP 5.x): OLE 의 BodyText/Section* 레코드에서 문단 글자만 읽는다(표는 칸 글자가 줄로 나온다)."""
    import olefile
    if not olefile.isOleFile(str(path)):
        raise ConversionError("HWP 5 형식이 아닙니다(HWP 3 이하 또는 손상)")
    ole = olefile.OleFileIO(str(path))
    header = ole.openstream("FileHeader").read()
    if header[36] & 2:
        raise ConversionError("암호가 걸린 HWP 는 변환할 수 없습니다")
    compressed = header[36] & 1
    texts = []
    sections = sorted((e for e in ole.listdir() if e[0] == "BodyText"), key=lambda e: int(re.sub(r"\D", "", e[1]) or 0))
    for entry in sections:
        data = ole.openstream(entry).read()
        if compressed:
            data = zlib.decompress(data, -15)
        i = 0
        while i + 4 <= len(data):
            h = int.from_bytes(data[i:i + 4], "little")
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = int.from_bytes(data[i:i + 4], "little"); i += 4
            if tag == 67:  # HWPTAG_PARA_TEXT
                texts.append(para_text(data[i:i + size]))
            i += size
    return "\n".join(texts), {"sections": len(sections)}


def hwpx_text(path: Path) -> tuple[str, dict]:
    _scripts()
    import hwpx_to_md
    return "\n".join(hwpx_to_md.convert(path)), {}


def plain_text(path: Path) -> tuple[str, dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc), {"encoding": enc}
        except UnicodeDecodeError:
            continue
    raise ConversionError("글자 인코딩을 알 수 없습니다(UTF-8 · CP949 아님)")


EXTRACT = {"pdf": pdf_text, "docx": docx_text, "hwp": hwp_text, "hwpx": hwpx_text, "md": plain_text, "txt": plain_text}


def normalize(text: str) -> str:
    """공통 후처리: pandoc 이스케이프(\\[ \\] 등) 제거, '부칙' 줄을 제목으로(부칙 제1조가 본문 제1조와 섞이지 않게), 줄끝 공백."""
    text = re.sub(r"\\([\[\]()*_#|<>])", r"\1", text)
    text = re.sub(r"(?m)^\s*부\s*칙\s*$", "## 부칙", text)
    return re.sub(r"[ \t]+$", "", text, flags=re.M).strip() + "\n"


def to_markdown(path: Path, fmt: str) -> tuple[str, dict]:
    if fmt not in EXTRACT:
        raise ConversionError(f"지원하지 않는 형식: {fmt}")
    text, report = EXTRACT[fmt](path)
    if not text.strip():
        raise ConversionError("추출한 글자가 없습니다")
    return normalize(text), {"format": fmt, **report}


# ── OCR 검수 도움 ───────────────────────────────────────────────
# 실측에서 나온 오인식 꼴: 줄 머리 원문자(①②③ → © @ 0 2)), 한글 문장 속 로마자 대문자 덩어리(규정은 → FAS)
_OCR_SUSPECT = re.compile(r"(?:^|\s)[©@®]|^(?:제\s*\d+\s*조\s*\([^)]*\)\s*)?[0-9]\)?\s+(?=[가-힣])|(?<![A-Za-z])[A-Z]{3,}(?![A-Za-z])")


def ocr_suspects(markdown: str, limit: int = 30) -> list[dict]:
    """OCR 로 읽은 변환본에서 원본과 대조할 줄(줄 번호 1부터). 목록 번호(1. 2.)와 영문만 있는 줄은 뺀다."""
    out = []
    for no, line in enumerate(markdown.splitlines(), 1):
        if not re.search(r"[가-힣]", line) or re.match(r"^\s*\d+\.\s", line):
            continue
        chars = re.sub(r"\s", "", line)
        if _OCR_SUSPECT.search(line) or len(re.findall(r"[가-힣]", chars)) < len(chars) * 0.4:   # 뒤쪽: 기호 위주 잡음 줄(큰 제목 · 도장)
            out.append({"line": no, "text": line[:80]})
            if len(out) >= limit:
                break
    return out


# ── 조문 미리보기 ───────────────────────────────────────────────
ARTICLE_LABEL = re.compile(r"^(부칙\s*)?제\s*\d+\s*조(의\s*\d+)?|^\[?\s*별표|^Q\s*\d+", re.I)


def preview(markdown: str) -> dict:
    """검수 화면에 보일 조문 목록과 경고. article_count 가 0 이면 승인할 수 없다(Spring)."""
    _scripts()
    from build_policy_json import split_articles
    arts = split_articles(markdown.splitlines())
    items, warnings, seen = [], [], set()
    for a in arts:
        label = a["label"]
        is_article = bool(ARTICLE_LABEL.match(label))
        key = re.sub(r"\s+", "", label.split("(")[0])
        if is_article and key in seen:
            warnings.append(f"같은 조문 번호가 두 번 나옴: {key}")
        seen.add(key)
        if is_article and len(a["text"]) < 15:
            warnings.append(f"본문이 아주 짧음: {label}")
        items.append({"label": label, "chars": len(a["text"]), "head": a["text"][:60], "article": is_article})
    count = sum(1 for x in items if x["article"])
    if count == 0:
        warnings.append("조문(제N조 · 별표 · Q)을 하나도 찾지 못했습니다 — 변환본의 조문 줄을 확인하세요")
    return {"article_count": count, "articles": items[:500], "warnings": warnings[:50]}
