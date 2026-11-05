"""답변 품질 채점 — 충실도(faithfulness) · 관련성(answer relevance). 실제 파이프라인을 돌려 받은 답변을 채점 모델이 읽고 점수를 낸다 (LLM 호출 있음).

정의는 RAGAS 를 따른다.
  충실도   = 답변을 사실 문장으로 나눈 뒤, 생성 모델에 준 컨텍스트(규정 조문 + 사례 체인)만으로 뒷받침되는 문장의 비율.
  관련성   = 답변만 보고 되짚어 만든 질문 3개와 원래 질문의 임베딩 코사인 유사도 평균. 답을 피한 답변은 0.
질문은 답변 점검표(data/answer_checks/*.json)의 것을 쓴다. 근거가 없어 답변을 만들지 않은 질문은 충실도 계산에서 뺀다(건수는 따로 낸다).

관련성은 임베딩 모델마다 척도가 다르다 — 같은 답변이 text-embedding-3-large 로는 0.67, text-embedding-ada-002 로는 0.90 이 나온다(ada-002 는 무관한 질문끼리도 0.80).
그래서 '엇갈린 쌍'(다른 질문의 답변에서 만든 질문과의 유사도)의 평균을 기준선으로 함께 낸다. 점수는 기준선과의 차이로 읽는다.

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../eval/answer_quality.py [점검표…] [--judge-model 모델] [--relevance-embedding 모델] [--json 결과경로] [--record]
      --record: 부서별 + 전체 1행을 eval_runs 에 남긴다(대시보드의 Faithfulness · Answer Relevance). 전체 점검표일 때만. DATABASE_URL(관리자 접속)이 필요하다
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))

from app.config import ai_mode, profile  # noqa: E402
from app.graph import pipeline  # noqa: E402
from app.llm import client as llm  # noqa: E402
from app.retrieval import embed  # noqa: E402

MARKER_RE = re.compile(r"\s*\[[PN]\d+\]")
N_QUESTIONS = 3

STATEMENTS_SYSTEM = """답변을 사실 문장으로 나눈다. JSON {"statements": ["…", …]} 만 낸다.
- 한 문장에는 사실 하나만 담는다. 대명사는 가리키는 말로 바꿔 그 문장만 읽어도 뜻이 통하게 쓴다.
- 답변에 있는 내용만 쓴다. 덧붙이거나 고치지 않는다.
- 인사말, 질문을 되풀이한 말, "도움이 되셨길 바랍니다" 같은 말은 뺀다. 사실 문장이 없으면 빈 목록을 낸다."""

VERDICT_SYSTEM = """각 문장이 컨텍스트만으로 뒷받침되는지 판정한다. JSON {"verdicts": [{"n": 1, "supported": true|false, "reason": "…"}, …]} 만 낸다.
- 컨텍스트에 적힌 내용에서 바로 따라 나오면 true. 컨텍스트에 없거나 컨텍스트와 다르면 false. 상식으로는 맞아도 컨텍스트에 없으면 false 다.
- "…에 대한 기준은 규정에 없다"는 문장은, 컨텍스트에 그 기준이 실제로 없거나 '기준이 없다'고 적혀 있으면 true 다.
- 숫자·기간·조건은 컨텍스트와 정확히 같아야 한다.
- 문장마다 하나씩, 주어진 번호 그대로 판정한다. reason 은 한 줄."""

QUESTIONS_SYSTEM = f"""답변만 보고, 이 답변이 대답하고 있는 질문을 {N_QUESTIONS}개 만든다. JSON {{"questions": ["…", …], "noncommittal": true|false}} 만 낸다.
- 질문은 답변의 내용 전체를 묻는 한 문장으로, 서로 다른 말로 쓴다.
- noncommittal 은 답변이 답을 피할 때만 true 다("모르겠습니다", "확인이 필요합니다"만 있는 답). "규정에 기준이 없다"고 사실을 밝히는 답은 false 다."""


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text).strip()


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def faithfulness_of(verdicts: list[dict], n_statements: int) -> float | None:
    """뒷받침된 문장 수 / 문장 수. 판정이 빠진 문장은 뒷받침되지 않은 것으로 센다. 문장이 없으면 None."""
    if not n_statements:
        return None
    ok = {v.get("n") for v in verdicts if v.get("supported") is True}
    return round(len([i for i in range(1, n_statements + 1) if i in ok]) / n_statements, 4)


def relevance_of(sims: list[float], noncommittal: bool) -> float:
    if noncommittal or not sims:
        return 0.0
    return round(sum(sims) / len(sims), 4)


def baseline_of(rows: list[dict]) -> float | None:
    """관련성의 기준선: 질문과, 다른 질문의 답변에서 만든 질문의 평균 유사도. 행에 벡터(_q, _g)가 있어야 한다."""
    n, sims = len(rows), []
    for i, r in enumerate(rows):
        other = rows[(i + n // 2) % n]
        if other["query"] != r["query"] and r.get("_q") and other.get("_g"):
            sims += [cosine(r["_q"], g) for g in other["_g"]]
    return round(sum(sims) / len(sims), 4) if sims else None


def summarize(rows: list[dict]) -> dict:
    """행들의 평균. 충실도는 답변을 만든 질문만, 관련성은 전부."""
    f = [r["faithfulness"] for r in rows if r["faithfulness"] is not None]
    rel = [r["answer_relevance"] for r in rows]
    return {"faithfulness": round(sum(f) / len(f), 4) if f else None,
            "answer_relevance": round(sum(rel) / len(rel), 4) if rel else None, "n": len(rows),
            "meta": {"faithfulness_n": len(f), "statements": sum(r["statements"] for r in rows), "supported": sum(r["supported"] for r in rows),
                     "noncommittal": sum(r["noncommittal"] for r in rows), "no_answer": sum(r["no_answer"] for r in rows)}}


def parse(buf: bytes) -> list[tuple[str, object]]:
    out = []
    for raw in buf.decode("utf-8").split("\n\n"):
        ev, data = None, []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if ev:
            out.append((ev, json.loads("\n".join(data))))
    return out


async def run_one(item: dict, prof, judge: str, seen: dict, emb_prof) -> dict:
    seen.clear()
    buf = b""
    async for chunk in pipeline.run(item["workspace_id"], None, item["query"]):
        buf += chunk
    blocks = parse(buf)
    answer = strip_markers("".join(b[1]["text"] for b in blocks if b[0] == "token"))
    context = seen.get("context")          # 생성 모델에 실제로 준 컨텍스트. 근거가 없어 생성하지 않았으면 None
    usage = llm.Usage()
    row = {"workspace_id": item["workspace_id"], "query": item["query"], "answer": answer, "no_answer": context is None,
           "faithfulness": None, "statements": 0, "supported": 0, "unsupported": []}
    if context is not None:
        st = (await llm._chat_json(prof, judge, STATEMENTS_SYSTEM, f"[질문]\n{item['query']}\n\n[답변]\n{answer}", usage)).get("statements") or []
        st = [s for s in st if isinstance(s, str) and s.strip()]
        verdicts = []
        if st:
            numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(st, 1))
            verdicts = (await llm._chat_json(prof, judge, VERDICT_SYSTEM, f"[컨텍스트]\n{context}\n\n[문장]\n{numbered}", usage)).get("verdicts") or []
        ok = {v.get("n") for v in verdicts if v.get("supported") is True}
        row.update(faithfulness=faithfulness_of(verdicts, len(st)), statements=len(st), supported=len([i for i in range(1, len(st) + 1) if i in ok]),
                   unsupported=[{"statement": s, "reason": next((v.get("reason") for v in verdicts if v.get("n") == i), None)}
                                for i, s in enumerate(st, 1) if i not in ok])
    gen = await llm._chat_json(prof, judge, QUESTIONS_SYSTEM, f"[답변]\n{answer}", usage)
    qs = [q for q in (gen.get("questions") or []) if isinstance(q, str) and q.strip()][:N_QUESTIONS]
    vecs = embed.embed(emb_prof, [item["query"], *qs]) if qs else None
    sims = [cosine(vecs[0], v) for v in vecs[1:]] if vecs else []
    row.update(answer_relevance=relevance_of(sims, bool(gen.get("noncommittal"))), noncommittal=bool(gen.get("noncommittal")), generated_questions=qs,
               _q=vecs[0] if vecs else None, _g=vecs[1:] if vecs else [])
    return row


def record(rows: list[dict], judge: str, prof, emb_model: str, baseline: float | None, version: str) -> int:
    """eval_runs 에 부서별 행과 전체(workspace_id NULL) 행을 넣는다. 넣은 행 수를 돌려준다."""
    import psycopg
    from psycopg.types.json import Jsonb
    groups: dict[int | None, list[dict]] = {None: rows}
    for r in rows:
        groups.setdefault(r["workspace_id"], []).append(r)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for ws, rs in groups.items():
            s = summarize(rs)
            conn.execute("""INSERT INTO eval_runs (workspace_id, dataset_version, mode, is_sample, faithfulness, answer_relevance, n_samples, meta)
                            VALUES (%s, %s, 'hybrid', false, %s, %s, %s, %s)""",
                         (ws, version, s["faithfulness"], s["answer_relevance"], s["n"],
                          Jsonb({**s["meta"], "judge_model": judge, "answer_model": prof.model_answer, "relevance_embedding": emb_model,
                                 "relevance_baseline": baseline})))
    return len(groups)


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--judge-model", default=None, help="채점 모델. 기본은 답변 모델. 답변 모델보다 상위 모델을 권한다")
    ap.add_argument("--relevance-embedding", default=None, help="관련성에 쓸 임베딩 모델. 기본은 검색에 쓰는 모델")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    prof = profile()
    if not (ai_mode() == "openai" and prof.has_key):
        print("채점은 openai 모드에서만 한다 (LLM 키 필요)"); return 1
    judge = a.judge_model or prof.model_answer
    emb_prof = dataclasses.replace(prof, embedding_model=a.relevance_embedding) if a.relevance_embedding else prof
    files = a.files or sorted((ROOT / "data" / "answer_checks").glob("*.json"))
    items = [it for f in files for it in json.loads(f.read_text(encoding="utf-8"))]
    # 생성 모델에 준 컨텍스트를 그대로 채점에 쓴다 — 이벤트의 근거 카드에는 사례 본문 요약과 규정 관계 표시가 없다
    seen: dict = {}
    render = llm._render_context

    def spy(policy_ctx, chain_ctx):
        seen["context"] = render(policy_ctx, chain_ctx)
        return seen["context"]
    llm._render_context = spy
    rows = []
    for it in items:
        r = await run_one(it, prof, judge, seen, emb_prof)
        rows.append(r)
        f = "  -  " if r["faithfulness"] is None else f"{r['faithfulness']:.2f}"
        print(f"충실도 {f} ({r['supported']}/{r['statements']})  관련성 {r['answer_relevance']:.2f}  ws{r['workspace_id']} {r['query']}")
        for u in r["unsupported"]:
            print(f"      ✗ {u['statement']}  ← {u['reason']}")
    s = summarize(rows)
    base = baseline_of(rows)
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    print(f"\n충실도 {s['faithfulness']} (문장 {s['meta']['supported']}/{s['meta']['statements']}, 답변 {s['meta']['faithfulness_n']}건)   "
          f"관련성 {s['answer_relevance']} ({s['n']}건, 답을 피한 답변 {s['meta']['noncommittal']}, 엇갈린 쌍 기준선 {base}, {emb_prof.embedding_model})   채점 모델 {judge}")
    if a.record:
        if a.files:
            print("--record 는 전체 점검표일 때만 쓴다"); return 1
        print(f"eval_runs 에 {record(rows, judge, prof, emb_prof.embedding_model, base, 'answer_checks v1')}행 기록 (부서별 + 전체)")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"summary": s, "judge_model": judge, "relevance_embedding": emb_prof.embedding_model, "relevance_baseline": base, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
