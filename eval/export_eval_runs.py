"""저장된 평가 결과(--json) → data/eval/eval_runs.json. LLM·임베딩 호출 없음.

hit_at_k.py · answer_quality.py 의 --json 결과를 --record 와 같은 행으로 바꾸고, 측정 시각(결과 파일이 쓰인 시각)을 붙인다.
운영 DB 는 이 파일을 bootstrap 의 eval 단계(scripts/load_eval_runs.py)로 받는다 — 서버에서 평가를 다시 돌리지 않는다.

실행: cd ai && uv run python ../eval/export_eval_runs.py --hit <hit.json> [--hit-mode rerank] --quality <q1.json> [--quality <q2.json>]
      [--source "실험 이름"] [--answer-model gpt-4o-mini] [--embedding-model text-embedding-3-large] [--out data/eval/eval_runs.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import answer_quality  # noqa: E402
import hit_at_k  # noqa: E402


def source_of(path: Path, label: str) -> str:
    """meta.source — 실험 폴더 기준 경로(실험/판/파일). label 이 있으면 앞에 붙인다."""
    rel = "/".join(path.resolve().parts[-3:])
    return f"{label} · {rel}" if label else rel


def measured_at(path: Path) -> str:
    """결과 파일이 쓰인 시각(평가가 끝난 시각). 초 단위, UTC."""
    return datetime.fromtimestamp(int(path.stat().st_mtime), timezone.utc).isoformat()


def from_hit(path: Path, mode: str | None, embedding_model: str, source: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mode = mode or ("rerank" if "rerank" in data["summary"] else "hybrid")
    if data["k"] != 5:
        raise SystemExit(f"{path.name}: k={data['k']} — eval_runs.hit_at_5 는 k=5 결과만 넣는다")
    at = measured_at(path)
    return [{**r, "run_at": at, "meta": {**r["meta"], "source": source_of(path, source)}}
            for r in hit_at_k.eval_rows(data["rows"], mode, 5, embedding_model, "evalset v1")]


def from_quality(path: Path, answer_model: str | None, source: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    model = data.get("answer_model") or answer_model
    if not model:
        raise SystemExit(f"{path.name}: 답변 모델 기록이 없다 — --answer-model 로 준다")
    at = measured_at(path)
    rows = answer_quality.eval_rows(data["rows"], data["judge_model"], model, data["relevance_embedding"],
                                    data.get("relevance_baseline"), "answer_checks v1")
    return [{**r, "run_at": at, "meta": {**r["meta"], "source": source_of(path, source)}} for r in rows]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--hit", type=Path, required=True)
    ap.add_argument("--hit-mode", default=None, help="기록할 열. 기본: 재순위 열이 있으면 rerank, 없으면 hybrid")
    ap.add_argument("--quality", type=Path, action="append", default=[])
    ap.add_argument("--source", default="", help="meta.source 앞에 붙일 설명 (뒤에는 실험/판/파일 경로가 붙는다)")
    ap.add_argument("--answer-model", default=None, help="품질 결과에 답변 모델 기록이 없을 때")
    ap.add_argument("--embedding-model", default="text-embedding-3-large")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "eval" / "eval_runs.json")
    a = ap.parse_args()
    rows = from_hit(a.hit, a.hit_mode, a.embedding_model, a.source)
    for q in a.quality:
        rows += from_quality(q, a.answer_model, a.source)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for r in rows:
        if r["workspace_id"] is None:
            v = r.get("hit_at_5") if r.get("hit_at_5") is not None else f"{r['faithfulness']} · {r['answer_relevance']}"
            print(f"{r['dataset_version']:<17} {r['mode']:<7} 전체 {v}  n={r['n_samples']}  {r['run_at']}")
    print(f"wrote {a.out} ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
