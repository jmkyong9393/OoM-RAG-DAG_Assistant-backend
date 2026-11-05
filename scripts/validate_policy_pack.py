"""data/policies/src/<slug>/ 규정 묶음 점검 — 원문 형식·메타 표준·회사(테넌트) 귀속·평가 질의 정합.

규정은 회사에 종속된다: meta.json 의 tenant·workspace_id 가 시드(data/seed)의 회사·부서와 맞아야 하고,
같은 부서 안에서 제목이 겹치면 개정(supersedes)이어야 한다(index_policy.py R8 이 앞 문서를 폐기하므로).
질의표는 본문만으로 답이 나와야 한다: 정답 조문·항·별표 행이 실제로 있고, 정답 요지의 숫자가 그 조문에 있어야 한다.

실행: uv run scripts/validate_policy_pack.py [slug ...] [--blocklist 파일] [--require-questions] [--quiet]
      (slug 생략 시 src/ 전체. --blocklist: 한 줄에 하나씩 적은 금지 고유명사 — 본문·질의·요지에 있으면 오류. 목록 파일은 레포에 두지 않는다)
종료 코드: 오류가 하나라도 있으면 1. 경고는 0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import types
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_policy_json as bpj  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "policies" / "src"
SEED = ROOT / "data" / "seed"

META_REQUIRED = ["slug", "tenant", "workspace_id", "title", "version", "doc_type", "source_type", "effective_from", "tags", "terms", "author", "summary"]
META_OPTIONAL = ["file_path", "supersedes"]
DOC_TYPES = {"규정", "지침", "FAQ", "매뉴얼", "약관", "방침", "고시"}
SOURCE_TYPES = {"pdf", "hwp", "markdown", "json"}
CIRC = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
Q_TYPES = ("기본", "의역", "표", "비교", "결합")
NUM_UNIT = re.compile(r"\d[\d,.]*\s*(?:영업일|개월|시간|퍼센트|만\s?원|천\s?원|일|년|회|분|원|%|배|주|건|명|세|자|초|급|개|GB|Gi|Mi|MB)")
# 한 회사의 규정으로 읽히지 않게 만드는 흔적(경고): 해외 시각·통화·행사, 영문 역할 약어
FOREIGN = {"해외 시각": r"\b(?:UTC|PST|GMT|EST)\b", "외화": r"달러|USD|\$\s?\d|(?<![가-힣])유로(?![가-힣])", "해외 고유": r"추수감사절|블랙프라이데이|GDPR",
           "영문 역할 약어": r"\b(?:EOC|IMOC|DBRE|IC|SRE|CISO|CPO)\b"}


def squash(s: str) -> str:
    return re.sub(r"[\s,]", "", s)


def seed_index() -> dict:
    ws = {w["id"]: w for w in json.loads((SEED / "workspaces.json").read_text(encoding="utf-8"))}
    tenants = {t["id"]: t["slug"] for t in json.loads((SEED / "tenants.json").read_text(encoding="utf-8"))}
    docs = json.loads((SEED / "policy_documents.json").read_text(encoding="utf-8"))
    return {"ws": ws, "tenants": tenants, "docs": docs}


def article_index(md_path: Path) -> tuple[list[dict], dict[str, str]]:
    """본문 조문 색인: '제7조' / '부칙제1조' / 'Q18' / '별표1' → 그 조문 글(라벨 포함)."""
    arts = bpj.split_articles(md_path.read_text(encoding="utf-8").splitlines())
    idx: dict[str, str] = {}
    for a in arts:
        lab, body = a["label"], a["label"] + " " + a["text"]
        m = re.match(r"(부칙 )?(제\d+조(?:의\d+)?)", lab)
        if m:
            idx[("부칙" if m.group(1) else "") + m.group(2)] = body
        m = re.match(r"(Q\d+)\.", lab)
        if m:
            idx[m.group(1)] = body
        m = re.search(r"별표\s*(\d+)", lab)
        if m:
            idx[f"별표{m.group(1)}"] = body
    return arts, idx


def check_body(slug: str, md_path: Path, E: list, W: list) -> dict:
    raw = md_path.read_bytes()
    if b"\r\n" in raw: E.append("본문: CRLF 줄바꿈 (LF 로)")
    if raw[:3] == b"\xef\xbb\xbf": E.append("본문: BOM")
    text = raw.decode("utf-8")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "): E.append("본문: 첫 줄이 '# 제목' 이 아님")
    bad = Counter()
    for l in lines:
        if re.match(r"^\s*#+\s*제\s*\d+\s*조", l): bad["조문 줄에 # (규칙 1)"] += 1
        if re.match(r"^\s*\|?\s*:?-{3,}", l): bad["표 구분선 |---| (규칙 5)"] += 1
        if "**" in l or re.search(r"\]\(https?://|<[a-zA-Z/][^>]*>|^\s*>|^```", l): bad["장식(굵게·링크·HTML·인용·코드) (규칙 7)"] += 1
        if re.match(r"^\s*[-*]\s+", l): bad["'- ' 목록 (규칙 7 — '1.' 만)"] += 1
        if re.search(r"[⑯-⑳]", l): bad["항 ⑮ 초과 (규칙 3)"] += 1
    for k, v in bad.items(): E.append(f"본문: {k} ×{v}")
    args = types.SimpleNamespace(slug=slug, workspace=0, title="", version="", doc_type="", source_type="markdown", file_path=None,
                                 effective_from=None, tags=[], supersedes=None, max_chars=700)
    chunks = bpj.build(md_path, args)["chunks"]
    ids = [c["id"] for c in chunks]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    if dup: E.append(f"변환: 청크 id 중복 {dup[:3]}")
    stray = sorted({c["article_label"] for c in chunks if re.match(rf"{re.escape(slug)}-s\d", c["id"]) and "별표" not in c["article_label"]})
    if stray: E.append(f"변환: 별표가 아닌데 순번 id(s) — 조문 줄 형식 확인 {stray[:3]}")
    over = [c["id"] for c in chunks if len(c["text"]) > 700]
    if over: E.append(f"변환: 700자 초과 청크 {over[:3]}")
    deep = sorted({c["article_label"] for c in chunks if int(c["id"].rsplit("-", 1)[1]) >= 3 and "별표" not in c["article_label"]})
    if deep: W.append(f"변환: 3조각 이상으로 나뉜 조문 {deep[:3]} (조를 나누는 편이 낫다)")
    renum = [i for i in ids if re.search(r"-[ap]\d+(?:-\d+)?[b-z]-\d+$", i)]
    if renum: W.append(f"변환: 조번호가 다시 나옴(b 접미) {renum[:3]}")
    for name, pat in FOREIGN.items():
        n = len(re.findall(pat, text))
        if n: W.append(f"본문: {name} 표현 ×{n} — 한 회사의 규정으로 읽히는지 확인")
    if not chunks: E.append("변환: 조문을 하나도 찾지 못함")
    return {"text": text, "chunks": chunks}


def check_meta(slug: str, d: Path, meta: dict, body_text: str, seeds: dict, E: list, W: list) -> None:
    miss = [k for k in META_REQUIRED if k not in meta]
    if miss: E.append(f"meta: 필수 키 없음 {miss}")
    extra = [k for k in meta if k not in META_REQUIRED + META_OPTIONAL]
    if extra: E.append(f"meta: 표준 밖 키 {extra} — 출처·실제 기업명 목록은 레포에 두지 않는다")
    if meta.get("slug") != slug: E.append(f"meta: slug '{meta.get('slug')}' ≠ 폴더 이름")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug): E.append("meta: slug 형식(소문자·숫자·하이픈)")
    if meta.get("doc_type") not in DOC_TYPES: E.append(f"meta: doc_type '{meta.get('doc_type')}' — {sorted(DOC_TYPES)} 중 하나")
    if meta.get("source_type") not in SOURCE_TYPES: E.append(f"meta: source_type '{meta.get('source_type')}'")
    ws = seeds["ws"].get(meta.get("workspace_id"))
    if not ws:
        E.append(f"meta: workspace_id {meta.get('workspace_id')} 가 시드에 없음")
    else:
        t = seeds["tenants"].get(ws["tenant_id"])
        if meta.get("tenant") != t: E.append(f"meta: tenant '{meta.get('tenant')}' ≠ 부서 {ws['id']} 의 회사 '{t}'")
        if not slug.startswith(f"{t}-") and meta.get("tenant") == t: W.append(f"meta: slug 가 회사 접두어 '{t}-' 로 시작하지 않음")
    has_pdf = (d / f"{slug}.pdf").exists()
    if meta.get("source_type") == "pdf" and not has_pdf: E.append("meta: source_type=pdf 인데 <slug>.pdf 없음")
    if has_pdf and meta.get("source_type") != "pdf": E.append("meta: PDF 가 있는데 source_type≠pdf")
    if has_pdf and meta.get("file_path") != f"policies/{slug}.pdf": E.append(f"meta: file_path 는 'policies/{slug}.pdf'")
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일부터\s*시행", body_text)
    if m:
        eff = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if meta.get("effective_from") != eff: E.append(f"meta: effective_from {meta.get('effective_from')} ≠ 부칙 시행일 {eff}")
    else:
        W.append("본문: 부칙 시행일 문장을 찾지 못함('YYYY년 M월 D일부터 시행')")
    if not (3 <= len(meta.get("tags") or []) <= 6): W.append(f"meta: tags {len(meta.get('tags') or [])}개 (3~6)")
    terms = meta.get("terms") or []
    absent = [t for t in terms if squash(t) not in squash(body_text)]
    if absent: W.append(f"meta: 본문에 없는 terms {absent[:5]} (본문 표기와 같아야 사전이 쓰인다)")


def check_collisions(metas: dict[str, dict], seeds: dict, errors: dict[str, list]) -> None:
    """같은 부서·같은 제목은 개정이어야 한다. 개정 사슬은 시드 ← data/policies/*.chunks.json ← src 순으로 이어질 수 있다."""
    by = defaultdict(list)
    for slug, m in metas.items():
        by[(m.get("workspace_id"), m.get("title"))].append(slug)
    edge = {slug: m["supersedes"] for slug, m in metas.items() if m.get("supersedes")}
    for f in (ROOT / "data" / "policies").glob("*.chunks.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("supersedes") and d["slug"] not in edge:
            edge[d["slug"]] = d["supersedes"]

    def chain(slug: str) -> set[str]:
        seen = set()
        while slug in edge and edge[slug] not in seen:
            slug = edge[slug]; seen.add(slug)
        return seen

    for (ws, title), slugs in by.items():
        if len(slugs) > 1:
            for s in slugs:
                errors[s].append(f"충돌: 부서 {ws} 에 제목 '{title}' 이 {slugs} — 적재하면 서로를 개정으로 보고 폐기한다")
        for sd in seeds["docs"]:
            if sd["workspace_id"] == ws and sd["title"] == title and sd.get("status") == "active":
                for s in slugs:
                    if sd["slug"] not in chain(s):
                        errors[s].append(f"충돌: 시드 '{sd['slug']}' 와 같은 부서·같은 제목 — 개정이면 supersedes 사슬로 이어져야 한다")


def parse_questions(md: str) -> list[dict]:
    rows = []
    for line in md.splitlines():
        if not line.startswith("|"): continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 5 or not c[0].isdigit(): continue
        rows.append({"no": int(c[0]), "type": c[1], "query": c[2], "answer": c[3], "gist": c[4]})
    return rows


def check_questions(slug: str, qpath: Path, idx: dict[str, str], body_text: str, block: list[str], E: list, W: list) -> None:
    rows = parse_questions(qpath.read_text(encoding="utf-8"))
    if len(rows) != 15: E.append(f"질의: {len(rows)}건 (15건)")
    types_ = Counter(next((t for t in Q_TYPES if r["type"].startswith(t)), "?") for r in rows)
    if types_.get("?"): E.append(f"질의: 유형은 {Q_TYPES} 로 시작 — {[r['no'] for r in rows if not r['type'].startswith(Q_TYPES)]}")
    has_table = any(k.startswith("별표") for k in idx)
    want = {"기본": 7, "의역": 4}
    for t, n in want.items():
        if types_.get(t, 0) < n - 1: W.append(f"질의: '{t}' {types_.get(t, 0)}건 (권장 {n})")
    if has_table and types_.get("표", 0) < 2: W.append(f"질의: '표' {types_.get('표', 0)}건 (별표가 있으면 2)")
    cited = set()
    for r in rows:
        tag = f"질의 #{r['no']}"
        if re.search(r"제\s*\d+\s*조|별표\s*\d", r["query"]): E.append(f"{tag}: 질의에 조문·별표 번호가 들어 있음")
        if not (8 <= len(r["query"]) <= 45): W.append(f"{tag}: 질의 길이 {len(r['query'])}자 (15~40 권장)")
        own = re.sub(r"\(\+.*?\)\s*$", "", r["answer"]).strip()
        refs = []
        for m in re.finditer(r"(부칙\s*)?제(\d+)조(?:의(\d+))?\s*([" + CIRC + r"]*)|(Q)\s*(\d+)|별표\s*(\d+)\s*([^,]*?)(?:\s*행)?(?=,|$)", own):
            if m.group(2):
                refs.append((("부칙" if m.group(1) else "") + f"제{m.group(2)}조" + (f"의{m.group(3)}" if m.group(3) else ""), m.group(4) or "", None))
            elif m.group(5):
                refs.append((f"Q{m.group(6)}", "", None))
            elif m.group(7):
                refs.append((f"별표{m.group(7)}", "", (m.group(8) or "").strip()))
        if not refs:
            E.append(f"{tag}: 정답 조문 표기를 읽지 못함 '{r['answer']}'"); continue
        texts = []
        for key, hang, rowkey in refs:
            art = idx.get(key)
            if art is None:
                E.append(f"{tag}: 정답 조문 {key} 가 본문에 없음"); continue
            cited.add(key); texts.append(art)
            for h in hang:
                if h not in art: E.append(f"{tag}: {key} 에 {h}항이 없음")
            if rowkey:
                parts = [p for p in re.split(r"[·/]", rowkey) if len(p) >= 2]
                if squash(rowkey) not in squash(art) and not (parts and all(squash(p) in squash(art) for p in parts)):
                    E.append(f"{tag}: {key} 에 '{rowkey}' 행이 없음")
        joined = squash(" ".join(texts))
        missing = [n for n in (squash(x) for x in NUM_UNIT.findall(r["gist"])) if n not in joined]
        if texts and missing:
            where = "문서 전체에도 없음" if any(n not in squash(body_text) for n in missing) else "다른 조문에 있음"
            E.append(f"{tag}: 정답 요지의 숫자 {missing} 가 인용 조문에 없음 ({where})")
        hit = [b for b in block if b in r["query"] or b in r["gist"]]
        if hit: E.append(f"{tag}: 금지 고유명사 {hit}")
    if rows and len(cited) < 10: W.append(f"질의: 정답이 조문 {len(cited)}개에 몰림 (10개 이상에 퍼지게)")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--blocklist", type=Path, default=None)
    ap.add_argument("--require-questions", action="store_true", help="질의표가 없으면 오류(기본은 경고)")
    ap.add_argument("--quiet", action="store_true", help="통과한 문서는 한 줄만")
    a = ap.parse_args()
    block = [l.strip() for l in a.blocklist.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if a.blocklist else []
    seeds = seed_index()
    dirs = sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "meta.json").exists() and (not a.slugs or p.name in a.slugs))
    errors: dict[str, list] = defaultdict(list); warns: dict[str, list] = defaultdict(list); metas: dict[str, dict] = {}; stats = {}
    for d in dirs:
        slug = d.name; E, W = errors[slug], warns[slug]
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")); metas[slug] = meta
        md = d / f"{slug}.md"
        if not md.exists():   # HWPX 등 다른 원문에서 만든 문서(고시)는 메타만 본다
            W.append("본문: <slug>.md 없음 — 형식·질의 점검 생략"); continue
        body = check_body(slug, md, E, W)
        check_meta(slug, d, meta, body["text"], seeds, E, W)
        hit = [b for b in block if b in body["text"]]
        if hit: E.append(f"본문: 금지 고유명사 {hit[:5]}")
        _, idx = article_index(md)
        q = d / f"{slug}.questions.md"
        if q.exists():
            check_questions(slug, q, idx, body["text"], block, E, W)
        else:
            (E if a.require_questions else W).append("질의: <slug>.questions.md 없음")
        stats[slug] = (len(body["chunks"]), len({c["article_label"] for c in body["chunks"]}))
    check_collisions(metas, seeds, errors)
    bad = 0
    for d in dirs:
        slug = d.name; E, W = errors[slug], warns[slug]; c, n = stats.get(slug, (0, 0))
        mark = "FAIL" if E else "ok  "
        bad += bool(E)
        m = metas[slug]
        print(f"{mark} {slug:<36} {m.get('tenant', '?'):<7} ws{m.get('workspace_id', '?'):<3} 청크 {c:<3} 조문 {n:<3} 오류 {len(E)} 경고 {len(W)}")
        if not (a.quiet and not E):
            for x in E: print(f"     오류  {x}")
            for x in W: print(f"     경고  {x}")
    print(f"\n{len(dirs)}종 중 통과 {len(dirs) - bad} · 실패 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
