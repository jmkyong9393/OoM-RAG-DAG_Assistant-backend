"""<slug>.questions.md → data/evalset/<slug>_v1.json 변환 (D-4 ④).

questions.md 표의 '정답 조문' 열을 변환 결과(<slug>.chunks.json)의 청크 id 로 매핑한다.
  제11조 ①②④            → {slug}-a11-* 중 해당 항(①②④)이 본문에 있는 청크
  제18조의2 ①②           → {slug}-a18-2-*
  Q18                     → {slug}-q18-* (FAQ)
  별표 2 설치 후 대형 가전·가구 행 → [별표 2] 청크 중 그 글자가 들어 있는 행(청크). 공백은 무시하고 비교
  (+ 제20조 ②)            → 같은 문서면 expect 에 추가, 다른 문서면 note 에만 남김
항을 안 쓴 조문은 그 조문의 청크 전부. 매핑이 0건이면 오류로 멈춘다(평가셋에 빈 정답이 들어가지 않게).

실행: uv run scripts/questions_to_evalset.py --meta data/policies/src/<slug>/meta.json [--out data/evalset]
      (make index 뒤 — <slug>.chunks.json 이 있어야 한다)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CIRC = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
ART = re.compile(r"제(\d+)조(?:의(\d+))?\s*([" + CIRC + r"]*)")
FAQ = re.compile(r"\bQ(\d+)\b")
TBL = re.compile(r"별표\s*(\d+)\s*(.*?)\s*행\s*$")


def parse_rows(md: str) -> list[dict]:
    rows = []
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        rows.append({"no": int(cells[0]), "type": cells[1], "query": cells[2], "answer": cells[3], "gist": cells[4]})
    return rows


def split_refs(answer: str) -> tuple[str, str]:
    """'제4조 ④ (+ 고객응대 근로자 보호 지침 제11조 ①)' → ('제4조 ④', '고객응대 근로자 보호 지침 제11조 ①')"""
    m = re.search(r"\(\+\s*(.*?)\)\s*$", answer)
    return (answer[: m.start()].strip(), m.group(1).strip()) if m else (answer.strip(), "")


def map_article(chunks: list[dict], slug: str, n: str, sub: str | None, hang: str) -> list[str]:
    prefix = f"{slug}-a{n}-" + (f"{sub}-" if sub else "")
    cand = [c for c in chunks if c["id"].startswith(prefix) and re.fullmatch(r"\d+", c["id"][len(prefix):])]
    if not hang or len(cand) <= 1:
        return [c["id"] for c in cand]
    return [c["id"] for c in cand if any(h in c["text"] for h in hang)]


def map_table(chunks: list[dict], k: str, key: str) -> list[str]:
    cand = [(c["id"], re.sub(r"\s", "", c["text"])) for c in chunks if c["article_label"].startswith(f"[별표 {k}]")]
    key = re.sub(r"\s", "", key)             # '경조금 행' ↔ 표의 '경조 금품' — 공백은 무시
    hit = [i for i, t in cand if key and key in t]
    if not hit and key:                      # '안내 문구·음성안내' → 조각 전부
        parts = [p for p in re.split(r"[·,/]", key) if len(p) >= 2]
        hit = [i for i, t in cand if parts and all(p in t for p in parts)]
    if not hit and len(cand) == 1:           # 별표가 한 청크면 행을 못 찾아도 정답은 그 청크 하나
        hit = [cand[0][0]]
    return hit


def map_refs(text: str, chunks: list[dict], slug: str) -> list[str]:
    ids: list[str] = []
    for part in re.split(r"[,、]|\s*·\s*(?=제\d+조|별표)", text):
        part = part.strip()
        if not part:
            continue
        mt = TBL.search(part)
        if mt:
            ids += map_table(chunks, mt.group(1), mt.group(2))
            continue
        for m in ART.finditer(part):
            ids += map_article(chunks, slug, m.group(1), m.group(2), m.group(3))
        for m in FAQ.finditer(part):
            ids += [c["id"] for c in chunks if c["id"].startswith(f"{slug}-q{m.group(1)}-")]
    return list(dict.fromkeys(ids))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "evalset")
    a = ap.parse_args()
    meta = json.loads(a.meta.read_text(encoding="utf-8"))
    slug, ws = meta["slug"], int(meta["workspace_id"])
    src = a.meta.parent / f"{slug}.questions.md"
    chunks_path = ROOT / "data" / "policies" / f"{slug}.chunks.json"
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))["chunks"]
    rows = parse_rows(src.read_text(encoding="utf-8"))
    if len(rows) != 15:
        print(f"{src}: 질의 {len(rows)}건 (15건 필요)")
    items, bad = [], []
    for r in rows:
        own, other = split_refs(r["answer"])
        expect = map_refs(own, chunks, slug)
        # (+ …) 가 같은 문서 조문이면(문서명 없이 '제N조'/'별표'로 시작) expect 에 추가
        if other and re.match(r"(제\d+조|별표)", other):
            expect += [x for x in map_refs(other, chunks, slug) if x not in expect]
        note = f"{r['type']} | {r['answer']} | {r['gist']}"
        if not expect:
            bad.append((r["no"], r["answer"]))
        items.append({"workspace_id": ws, "kind": "policy", "query": r["query"], "expect": [f"c:{x}" for x in expect], "note": note})
        print(f"  {r['no']:>2} {r['type']:<3} {r['answer']:<48} → {expect}")
    if bad:
        print(f"매핑 실패 {len(bad)}건: {bad}")
        return 1
    a.out.mkdir(parents=True, exist_ok=True)
    dest = a.out / f"{slug}_v1.json"
    body = ",\n".join("  " + json.dumps(it, ensure_ascii=False) for it in items)   # seed_v1.json 과 같은 한 줄 한 건
    dest.write_text("[\n" + body + "\n]\n", encoding="utf-8")
    print(f"wrote {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}: {len(items)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
