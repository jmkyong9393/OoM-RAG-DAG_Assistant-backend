"""data/policies/src/<slug>/ 규정 묶음 점검 — 원문 형식·메타 표준·회사(테넌트) 귀속·평가 질의 정합.

규정은 회사에 종속된다: meta.json 의 tenant·workspace_id 가 시드(data/seed)의 회사·부서와 맞아야 하고,
같은 부서 안에서 제목이 겹치면 개정(supersedes)이어야 한다(index_policy.py R8 이 앞 문서를 폐기하므로).
질의표는 본문만으로 답이 나와야 한다: 정답 조문·항·별표 행이 실제로 있고, 정답 요지의 숫자가 그 조문에 있어야 한다.
본문이 가리키는 조문 · 항 · 같은 회사의 다른 규정이 실제로 있어야 하고(모르는 문서 이름은 경고), 부서 호칭은 회사 표준을 따른다.
원문 PDF 가 있는 문서는 변환본(data/policies/<slug>.chunks.json)의 조각마다 쪽 번호가 있어야 한다.

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
META_OPTIONAL = ["file_path", "supersedes", "ask_workspace_id"]   # ask_workspace_id: 평가 질의를 묻는 부서(문서가 전사 공통일 때)
DOC_TYPES = {"규정", "지침", "FAQ", "매뉴얼", "약관", "방침", "고시"}
SOURCE_TYPES = {"pdf", "hwp", "markdown", "json"}
CIRC = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
Q_TYPES = ("기본", "의역", "표", "비교", "결합", "교차")   # 교차 = 다른 문서의 조문과 함께 답하는 질의
NUM_UNIT = re.compile(r"\d[\d,.]*\s*(?:영업일|개월|시간|퍼센트|만\s?원|천\s?원|일|년|회|분|원|%|배|주|건|명|세|자|초|급|개|GB|Gi|Mi|MB)")
# 한 회사의 규정으로 읽히지 않게 만드는 흔적(경고): 해외 시각·통화·행사, 영문 역할 약어
FOREIGN = {"해외 시각": r"\b(?:UTC|PST|GMT|EST)\b", "외화": r"달러|USD|\$\s?\d|(?<![가-힣])유로(?![가-힣])", "해외 고유": r"추수감사절|블랙프라이데이|GDPR",
           "영문 역할 약어": r"\b(?:EOC|IMOC|DBRE|IC|SRE|CISO|CPO)\b"}
# 회사별 표준 호칭(오류): 같은 기능을 회사마다 한 이름으로 부른다
NAMING = {"retail": {r"피플팀": "인사팀", r"인사\s?(?:담당\s?)?부서": "인사팀"},
          "it": {r"인사팀": "피플팀", r"인사\s?(?:담당\s?)?부서": "피플팀"}}
# 교차참조: "제N조"의 출처를 앞말로 가린다 — 법령(외부)은 보지 않고, 이 문서 · 같은 회사의 다른 규정이면 조문 · 항이 있어야 한다
XREF = re.compile(r"(부칙\s*)?제(\d+)조(?:의(\d+))?((?:\s*[" + CIRC + r"])*)")
XREF_EXT = re.compile(r"(?:법|법률|령|규칙|고시|협약|가이드라인)」?$|」$|같은\s*(?:법|법률|령|영|시행령|규칙|고시)$")
XREF_JOIN = re.compile(r"^[\s,·ㆍ및와과또는부터에서~\-" + CIRC + r"제항호목의]*$")   # "제3조 및 제5조 ②" 처럼 앞 출처를 잇는 말
DOC_SUFFIX = r"(?:규정|지침|운영지침|운영규정|관리규정|규칙|계획|규범|행동규범|규약|매뉴얼|편람|방침|FAQ)"
DOC_NAME = re.compile(r"((?:[가-힣A-Za-z·]+\s)?[가-힣A-Za-z·]*" + DOC_SUFFIX + r")(?=에\s*따|[을를]\s*따|에서\s*정|[이가]\s*정|에\s*정|[으]?로\s*정)")
DOC_GENERIC = re.compile(r"^(?:(?:이|본|각|관계|관련|해당|다른|같은|위|회사|사내|내부|그|하위|종전|부서의|계열사의|외부|별도)\s?)?" + DOC_SUFFIX + r"$")


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


def library(seeds: dict, dirs: list[Path]) -> dict:
    """회사별로 참조할 수 있는 문서: 제목 → slug, slug → 조문 색인. src 전체(점검 대상과 상관없이) · 점검 폴더 · 시드."""
    titles: dict[str, dict[str, str]] = defaultdict(dict); idx: dict[str, dict] = {}
    for d in [*SRC.glob("*/"), *dirs]:
        if not (d / "meta.json").exists(): continue
        m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        titles[m.get("tenant")][m.get("title")] = m["slug"]
        if (d / f"{m['slug']}.md").exists(): idx[m["slug"]] = article_index(d / f"{m['slug']}.md")[1]
    for sd in seeds["docs"]:   # 시드 문서는 원문 조문 색인 없이 이름만 안다
        t = seeds["tenants"].get(seeds["ws"].get(sd.get("workspace_id"), {}).get("tenant_id"))
        if t: titles[t].setdefault(sd["title"], sd["slug"])
    return {"titles": titles, "idx": idx}


def ref_source(lib: dict, tenant: str, own: str, before: str) -> str:
    """'제N조' 앞말로 출처를 가린다: slug · 'EXT'(법령) · 'SAME'(같은 규정) · 'UNKNOWN:이름'."""
    b = before.rstrip()
    for title, slug in sorted(lib["titles"].get(tenant, {}).items(), key=lambda x: -len(x[0] or "")):
        if title and (b.endswith(title) or b.endswith(f"「{title}」")):
            return slug
    if re.search(r"(?:이|본)\s*" + DOC_SUFFIX + r"$", b): return own
    if XREF_EXT.search(b): return "EXT"
    if re.search(r"같은\s*" + DOC_SUFFIX + r"$", b): return "SAME"
    last = re.search(r"([가-힣A-Za-z·」]+)$", b)
    if last and re.search(DOC_SUFFIX + r"$", last.group(1)): return "UNKNOWN:" + last.group(1)
    return own


def check_cross_refs(slug: str, tenant: str, text: str, lib: dict, E: list, W: list) -> None:
    """다른 조문 · 다른 규정을 가리키는 말이 실제로 있는 조문 · 항 · 문서인가. 회사별 표준 호칭."""
    known = lib["titles"].get(tenant, {})
    article = ""   # 지금 조문의 앞부분 — '같은 규정'은 그 안에서 마지막으로 이름이 나온 문서다
    for ln, line in enumerate(text.splitlines(), 1):
        head = re.match(r"^(부칙\s*)?제\d+조(?:의\d+)?\([^)]*\)", line)
        if head or line.startswith("#"): article = ""
        body = line[head.end():] if head else line
        prev, prev_end = None, 0
        for m in XREF.finditer(body):
            gap = body[prev_end:m.start()]
            src = prev if prev is not None and XREF_JOIN.match(gap) else ref_source(lib, tenant, slug, body[:m.start()])
            if src == "SAME":
                seen = article + " " + body[:m.start()]
                pos = {s: seen.rfind(t) for t, s in known.items() if t and t in seen}
                src = max(pos, key=pos.get) if pos else "UNKNOWN:" + re.search(r"같은\s*\S+$", body[:m.start()].rstrip()).group(0)
            prev, prev_end = src, m.end()
            if src == "EXT": continue
            # 표 칸 안의 '제N조'는 그 칸이 조문 번호만일 때만 이 문서로 본다 — 근거 법령 칸은 법 이름 없이 조문만 적기도 한다
            if line.startswith("|") and src == slug and not re.search(r"\|\s*" + re.escape(m.group(0).strip()) + r"\s*(?:\||$)", line): continue
            key = ("부칙" if m.group(1) else "") + f"제{m.group(2)}조" + (f"의{m.group(3)}" if m.group(3) else "")
            if src.startswith("UNKNOWN"):
                W.append(f"참조 {ln}행: '{src[8:]} {key}' — 어느 문서인지 모름(같은 회사 규정 제목과 다름)"); continue
            art = lib["idx"].get(src)
            if art is None: continue   # 원문이 없는 시드 문서
            where = "이 문서" if src == slug else f"'{src}'"
            if key not in art:
                E.append(f"참조 {ln}행: {key} 가 {where}에 없음"); continue
            for h in re.findall(f"[{CIRC}]", m.group(4) or ""):
                if h not in art[key]: E.append(f"참조 {ln}행: {key} {h} 가 {where}에 없음")
        article += " " + body
        for m in DOC_NAME.finditer(line):
            name = m.group(1).strip()
            if DOC_GENERIC.match(name) or re.search(r"법\s|시행규칙$", name) or re.search(r"별표\s*\d+\s*의\s*$", line[:m.start()]): continue
            if any(t and line[:m.end()].endswith(t) for t in known): continue   # 제목 전체가 앞에 있다
            W.append(f"참조 {ln}행: '{name}' — 같은 회사에 이런 제목의 규정이 없음(외부 기준이면 그대로 둔다)")
    for pat, std in NAMING.get(tenant, {}).items():
        for ln, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(pat, line):
                E.append(f"호칭 {ln}행: '{m.group(0)}' → 이 회사는 '{std}'")


def check_pdf_pages(slug: str, d: Path, E: list) -> None:
    """원문 PDF 가 있으면 변환본의 조각마다 쪽 번호가 있어야 한다(본문만 고치고 PDF 재생성 · 쪽 번호 채우기를 빠뜨린 경우)."""
    pdf, cj = d / f"{slug}.pdf", ROOT / "data" / "policies" / f"{slug}.chunks.json"
    if not pdf.exists() or not cj.exists(): return
    chunks = json.loads(cj.read_text(encoding="utf-8")).get("chunks", [])
    empty = [c["id"] for c in chunks if c.get("page") is None]
    if empty: E.append(f"PDF: 원문 PDF 가 있는데 쪽 번호가 빈 조각 {len(empty)}/{len(chunks)}개 {empty[:2]} — md_to_pdf.py 와 pdf_locate.py --write 를 다시")


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
    if len(rows) < 15: E.append(f"질의: {len(rows)}건 (15건 이상)")
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
        own = re.sub(r"\(=.*?\)\s*$", "", own).strip()   # (= 다른 문서 제N조) — 어느 쪽이든 정답. 다른 문서라 여기서는 보지 않는다
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
    ap.add_argument("--src", type=Path, default=None, help="점검할 폴더 (기본 data/policies/src). 개정판은 data/policies/revisions")
    a = ap.parse_args()
    block = [l.strip() for l in a.blocklist.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if a.blocklist else []
    seeds = seed_index()
    scan = a.src.resolve() if a.src else SRC
    dirs = sorted(p for p in scan.iterdir() if p.is_dir() and (p / "meta.json").exists() and (not a.slugs or p.name in a.slugs))
    errors: dict[str, list] = defaultdict(list); warns: dict[str, list] = defaultdict(list); metas: dict[str, dict] = {}; stats = {}
    lib = library(seeds, dirs)
    for d in dirs:
        slug = d.name; E, W = errors[slug], warns[slug]
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")); metas[slug] = meta
        md = d / f"{slug}.md"
        external = meta.get("doc_type") == "고시" or meta.get("source_type") in ("hwp", "json")
        if external or not md.exists():   # 법령·고시처럼 회사가 쓰지 않은 외부 기준 문서는 원문 형식을 고칠 수 없다 — 메타와 회사 귀속만 본다
            miss = [k for k in META_REQUIRED if k not in meta]
            if miss: E.append(f"meta: 필수 키 없음 {miss}")
            extra = [k for k in meta if k not in META_REQUIRED + META_OPTIONAL]
            if extra: E.append(f"meta: 표준 밖 키 {extra}")
            ws = seeds["ws"].get(meta.get("workspace_id"))
            if not ws: E.append(f"meta: workspace_id {meta.get('workspace_id')} 가 시드에 없음")
            elif meta.get("tenant") != seeds["tenants"].get(ws["tenant_id"]): E.append(f"meta: tenant '{meta.get('tenant')}' ≠ 부서 {ws['id']} 의 회사")
            if md.exists():
                args = types.SimpleNamespace(slug=slug, workspace=0, title="", version="", doc_type="", source_type="markdown", file_path=None,
                                             effective_from=None, tags=[], supersedes=None, max_chars=700)
                ch = bpj.build(md, args)["chunks"]
                stats[slug] = (len(ch), len({c["article_label"] for c in ch}))
            W.append("외부 기준 문서 — 원문 형식·질의 점검 생략")
            continue
        body = check_body(slug, md, E, W)
        check_meta(slug, d, meta, body["text"], seeds, E, W)
        hit = [b for b in block if b in body["text"]]
        if hit: E.append(f"본문: 금지 고유명사 {hit[:5]}")
        _, idx = article_index(md)
        check_cross_refs(slug, meta.get("tenant"), body["text"], lib, E, W)
        check_pdf_pages(slug, d, E)
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
