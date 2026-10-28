"""규정 원문(Markdown·텍스트) → data/policies/<slug>.chunks.json 변환기.

조문 단위로 자른다 (data/policies/README.md 형식):
  - 법령·규정: '제12조(원격접속 신청)' / '제12조의2' 줄에서 새 조문 시작. 긴 조문은 항(①②③…) 경계에서 max-chars 이하로 분할, article_label 은 같게
  - FAQ: 'Q18.' 또는 'Q18 ' 로 시작하는 줄에서 새 항목 시작
  - Markdown: '## ' 제목 줄도 조문 경계로 취급 (제목이 label)
  - '별표' 블록은 하나의 조문으로 취급. 표('| 셀 | 셀 |' 행, hwpx_to_md.py 출력)는 행 경계에서 분할
청크 id 는 결정적: {slug}-a{조번호}-{seq} (조번호가 없으면 s{순번}).

실행: uv run scripts/build_policy_json.py 원문.md --slug refund-policy-v4 --workspace 3 --title "환불규정" --version v4.0 \
        [--doc-type 규정] [--source-type markdown] [--supersedes refund-policy-v3] [--effective-from 2026-11-01] [--max-chars 700] [--out data/policies]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTICLE_RE = re.compile(r"^\s*(제\s*\d+\s*조(?:의\s*\d+)?)\s*(?:\(([^)]*)\)|（([^）]*)）)?\s*(.*)$")
FAQ_RE = re.compile(r"^\s*(Q\s*\d+)[.\s)]\s*(.*)$", re.I)
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
TABLE_RE = re.compile(r"^\s*\[?\s*별표\s*\d*\s*\]?\s*(.*)$")
PARA_RE = re.compile(r"(?=[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮])")
NUM_RE = re.compile(r"\d+")


def split_articles(lines: list[str]) -> list[dict]:
    """줄 목록 → [{label, title, text}]. 조문 경계 전까지의 줄을 하나로 모은다."""
    out: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            cur["text"] = re.sub(r"[ \t]+", " ", cur["text"]).strip()
            out.append(cur)
        cur = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if cur is not None:
                cur["text"] += "\n"
            continue
        m = ARTICLE_RE.match(line)
        f = FAQ_RE.match(line) if not m else None
        h = HEADING_RE.match(line) if not (m or f) else None
        t = TABLE_RE.match(line) if not (m or f or h) else None
        if m:
            flush()
            label = re.sub(r"\s+", "", m.group(1))
            title = (m.group(2) or m.group(3) or "").strip()
            cur = {"label": f"{label} ({title})" if title else label, "title": title or None, "text": m.group(4).strip()}
        elif f:
            flush()
            cur = {"label": re.sub(r"\s+", "", f.group(1).upper()) + ". " + f.group(2).strip(), "title": f.group(2).strip() or None, "text": ""}
        elif h:
            flush()
            cur = {"label": h.group(1).strip(), "title": h.group(1).strip(), "text": ""}
        elif t:
            flush()
            cur = {"label": line.strip(), "title": t.group(1).strip() or None, "text": ""}
        else:
            if cur is None:
                cur = {"label": "서문", "title": None, "text": ""}
            cur["text"] += ("\n" if cur["text"] and not cur["text"].endswith("\n") else "") + line.strip()
    flush()
    return out


def split_long(text: str, max_chars: int) -> list[str]:
    """항(①②…) 경계에서 max_chars 이하 조각으로. 항 하나가 그보다 길면 문장 경계로.
    표('| … |' 행)가 들어 있으면 행 경계에서 자른다 — 한 행이 쪼개지지 않게."""
    if re.search(r"^\|", text, re.M):
        return _split_rows(text, max_chars)
    text = text.replace("\n", " ").strip()
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in PARA_RE.split(text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in parts:
        if len(p) > max_chars:  # 한 항이 너무 길면 문장 단위로
            for s in re.split(r"(?<=[.。])\s+", p):
                if len(buf) + len(s) + 1 > max_chars and buf:
                    chunks.append(buf.strip()); buf = ""
                buf += (" " if buf else "") + s
            continue
        if len(buf) + len(p) + 1 > max_chars and buf:
            chunks.append(buf.strip()); buf = ""
        buf += (" " if buf else "") + p
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def _split_rows(text: str, max_chars: int) -> list[str]:
    """줄(표 행·문단) 단위로 max_chars 이하로 묶는다. 한 줄이 그보다 길면 그 줄만 문장 경계로 더 자른다."""
    units = [l.strip() for l in text.splitlines() if l.strip()]
    chunks: list[str] = []
    buf = ""
    for u in units:
        if len(u) > max_chars:
            if buf:
                chunks.append(buf); buf = ""
            chunks.extend(split_long(u.strip("| ").replace(" | ", " / "), max_chars))
            continue
        if len(buf) + len(u) + 1 > max_chars and buf:
            chunks.append(buf); buf = ""
        buf += ("\n" if buf else "") + u
    if buf:
        chunks.append(buf)
    return chunks


def build(source: Path, args) -> dict:
    lines = source.read_text(encoding="utf-8").splitlines()
    articles = split_articles(lines)
    chunks = []
    for i, a in enumerate(articles, 1):
        nums = NUM_RE.findall(a["label"])
        if a["label"].startswith("제") and nums:            # 제12조 → a12, 제2조의2 → a2-2
            key = f"a{nums[0]}" + (f"-{nums[1]}" if "조의" in a["label"] and len(nums) > 1 else "")
        elif a["label"].upper().startswith("Q") and nums:    # Q18 → q18
            key = f"q{nums[0]}"
        else:                                                # 제목·별표·서문 → 순번
            key = f"s{i}"
        for seq, piece in enumerate(split_long(a["text"], args.max_chars), 1):
            chunks.append({"id": f"{args.slug}-{key}-{seq}", "article_label": a["label"], "article_title": a["title"], "page": None, "text": piece})
    doc = {"slug": args.slug, "workspace_id": args.workspace, "title": args.title, "version": args.version, "doc_type": args.doc_type,
           "source_type": args.source_type, "file_path": args.file_path, "effective_from": args.effective_from, "tags": args.tags or [],
           "meta": {"source_file": source.name, "converter": "build_policy_json.py"}, "chunks": chunks}
    if args.supersedes:
        doc["supersedes"] = args.supersedes
    return doc


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("--slug", required=True); ap.add_argument("--workspace", type=int, required=True)
    ap.add_argument("--title", required=True); ap.add_argument("--version", required=True)
    ap.add_argument("--doc-type", default="규정"); ap.add_argument("--source-type", default="markdown", choices=["pdf", "hwp", "markdown", "json"])
    ap.add_argument("--file-path", default=None); ap.add_argument("--effective-from", default=None); ap.add_argument("--supersedes", default=None)
    ap.add_argument("--tags", nargs="*", default=None); ap.add_argument("--max-chars", type=int, default=700)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "policies")
    a = ap.parse_args()
    doc = build(a.source, a)
    if not doc["chunks"]:
        print("조문을 하나도 찾지 못했습니다 (제N조 / Q N / ## 제목 패턴 필요)"); return 1
    a.out.mkdir(parents=True, exist_ok=True)
    dest = a.out / f"{a.slug}.chunks.json"
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    labels = [c["article_label"] for c in doc["chunks"]]
    print(f"wrote {dest.relative_to(ROOT)}: {len(doc['chunks'])} chunks from {len(set(labels))} articles")
    for c in doc["chunks"][:5]:
        print(f"  {c['id']:<32} {c['article_label'][:28]:<28} {len(c['text'])}자")
    if len(doc["chunks"]) > 5:
        print(f"  … (+{len(doc['chunks']) - 5})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
