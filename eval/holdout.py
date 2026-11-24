"""보류 질문 블라인드 평가 — 고치는 데 쓴 질문의 점수로 품질을 주장하지 않기 위한 도구.

절차: seal(코드를 고치기 전에 질문을 골라 해시로 봉인) → 두 코드로 answer_checks.py 를 각각 돌려 답을 만든다
      → verify(봉인 해시 확인) → pack(A/B 를 섞은 검토 자료 + 따로 두는 매핑) → 맥락 없는 검토자가 자료만 보고 판정
      → unblind(판정을 매핑에 맞춰 버전별로 센다). 연 보류셋으로 다시 튜닝하지 않는다 — 다음 검증은 새로 봉인한다.

실행(ai 폴더에서, DB 가 필요한 것은 pack 뿐):
  uv run python ../eval/holdout.py seal --out 봉인.json --exclude 쓴질문.json ... [--n-policy 16 --n-case 8 --seed 1]
  uv run python ../eval/holdout.py verify --sealed 봉인.json
  uv run --extra graph python ../eval/holdout.py pack --sealed 봉인.json --a 이전.json --b 이후.json --out 검토자료.json --key 매핑.json
  uv run python ../eval/holdout.py unblind --key 매핑.json --verdicts 판정.json   # 판정: {"번호": "A"|"B"|"같음"}
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _queries(path: str) -> set[str]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = d if isinstance(d, list) else d.get("rows", [])
    return {x["query"] for x in rows if isinstance(x, dict) and "query" in x}


def seal(a) -> None:
    used = set().union(*(_queries(p) for p in a.exclude)) if a.exclude else set()
    pool = [{**x, "src": Path(f).name} for f in sorted(glob.glob(str(ROOT / "data" / "evalset" / "*_v1.json")))
            for x in json.loads(Path(f).read_text(encoding="utf-8")) if x["query"] not in used]
    by = collections.defaultdict(list)
    for x in pool:
        by[(x["workspace_id"], x["kind"])].append(x)
    rnd = random.Random(a.seed)
    picked = []
    for kind, n in (("policy", a.n_policy), ("troubleshoot", a.n_case)):   # 부서가 고르게 돌아가며 하나씩
        keys = sorted(k for k in by if k[1] == kind)
        rnd.shuffle(keys)
        while sum(1 for p in picked if p["kind"] == kind) < n and any(by[k] for k in keys):
            for k in keys:
                if by[k] and sum(1 for p in picked if p["kind"] == kind) < n:
                    picked.append(by[k].pop(rnd.randrange(len(by[k]))))
    body = json.dumps(picked, ensure_ascii=False, indent=1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(body, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    Path(a.out).with_suffix(".sha256").write_text(digest + "\n", encoding="utf-8")
    Path(a.out).with_name(Path(a.out).stem + "_질문.json").write_text(
        json.dumps([{"workspace_id": p["workspace_id"], "query": p["query"]} for p in picked], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"봉인 {len(picked)}문항 (후보 {len(pool)}, 제외 {len(used)}) sha256 {digest}")


def verify(a) -> bool:
    body = Path(a.sealed).read_bytes()
    want = Path(a.sealed).with_suffix(".sha256").read_text(encoding="utf-8").strip()
    ok = hashlib.sha256(body).hexdigest() == want
    print("봉인 그대로" if ok else "봉인이 바뀌었다 — 이 보류셋으로 판정하지 않는다")
    return ok


def _evidence(conn, q: dict, chunks: dict) -> list[str]:
    out = []
    for e in q.get("expect", []):
        if e.startswith("c:"):
            lab, t = chunks.get(e[2:], ("?", ""))
            out.append(f"[조문 {lab}] {t[:700]}")
        elif e.startswith("n:"):
            r = conn.execute("SELECT type, title, body FROM log_nodes WHERE id = %s", (int(e[2:]),)).fetchone()
            if r:
                out.append(f"[사례 기록 {r['type']}] {r['title']} — {(r['body'] or '')[:300]}")
    return out


def pack(a) -> None:
    if not verify(a):
        sys.exit(1)
    sys.path.insert(0, str(ROOT / "ai"))
    from app.retrieval import db  # noqa: E402
    qs = json.loads(Path(a.sealed).read_text(encoding="utf-8"))
    ans = {side: {r["query"]: r["answer"] for r in json.loads(Path(p).read_text(encoding="utf-8"))["rows"]} for side, p in (("a", a.a), ("b", a.b))}
    chunks = {}
    for f in glob.glob(str(ROOT / "data" / "policies" / "*.chunks.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for c in d.get("chunks", d if isinstance(d, list) else []):
            chunks[c["id"]] = (c.get("article_label", ""), c.get("text", ""))
    rnd = random.Random(a.seed)
    items, key = [], {}
    with db.connect() as conn:
        for i, q in enumerate(qs, 1):
            flip = rnd.random() < 0.5
            first, second = ("b", "a") if flip else ("a", "b")
            key[i] = {"A": first, "B": second, "query": q["query"], "유형": q.get("유형") or q.get("kind")}
            items.append({"번호": i, "질문": q["query"], "평가셋 메모": q.get("note"), "정답 근거": _evidence(conn, q, chunks),
                          "답변A": ans[first][q["query"]], "답변B": ans[second][q["query"]]})
    Path(a.out).write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(a.key).write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    same = sum(1 for q in qs if ans["a"][q["query"]].strip() == ans["b"][q["query"]].strip())
    print(f"검토 자료 {len(items)}문항 (글자까지 같은 답 {same}) — 매핑 {a.key} 는 검토자에게 주지 않는다")


def unblind(a) -> None:
    key = json.loads(Path(a.key).read_text(encoding="utf-8"))
    verdicts = json.loads(Path(a.verdicts).read_text(encoding="utf-8"))
    win, by = collections.Counter(), collections.defaultdict(collections.Counter)
    for n, v in verdicts.items():
        k = key[str(n)]
        who = k[v] if v in ("A", "B") else "같음"
        win[who] += 1
        by[k.get("유형")][who] += 1
    print("이전(a) / 이후(b) / 같음:", dict(win))
    for t, c in sorted(by.items(), key=lambda x: str(x[0])):
        print(f"  {t}: {dict(c)}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal")
    s.add_argument("--out", required=True)
    s.add_argument("--exclude", nargs="*", default=[])
    s.add_argument("--n-policy", type=int, default=16)
    s.add_argument("--n-case", type=int, default=8)
    s.add_argument("--seed", type=int, default=1)
    v = sub.add_parser("verify")
    v.add_argument("--sealed", required=True)
    p = sub.add_parser("pack")
    for k in ("--sealed", "--a", "--b", "--out", "--key"):
        p.add_argument(k, required=True)
    p.add_argument("--seed", type=int, default=7)
    u = sub.add_parser("unblind")
    u.add_argument("--key", required=True)
    u.add_argument("--verdicts", required=True)
    a = ap.parse_args()
    {"seal": seal, "verify": lambda x: sys.exit(0 if verify(x) else 1), "pack": pack, "unblind": unblind}[a.cmd](a)


if __name__ == "__main__":
    main()
