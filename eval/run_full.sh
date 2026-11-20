#!/usr/bin/env bash
# 전체 측정 — 검색(재순위 포함) → 답변 점검표 → 개정 상태 → 답변 품질. 실제 AI 를 부른다(한 번에 약 2달러).
# 검색과 답변 품질은 끝나는 대로 eval_runs 에 기록해(--record) 대시보드가 바로 바뀐다. 끝에 배포용 파일 data/eval/eval_runs.json 을 다시 만든다.
# 사용: 레포 루트에서  bash eval/run_full.sh <결과를 둘 폴더>     (make eval-full OUT=<폴더>)
# 결과(json)와 출력(log)을 그 폴더에 남긴다. 결과 파일이 이미 있는 단계는 건너뛴다 — 중간에 끊겼으면 같은 폴더로 다시 돌린다.
# 환경: JUDGE_MODEL(답변 품질의 채점 모델, 기본 gpt-4.1). 개정 상태 단계는 개정판이 적재된 DB(oom_eval)가 있어야 한다.
[ -n "$1" ] || { echo "사용: bash eval/run_full.sh <결과를 둘 폴더>"; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$1" && OUT="$(cd "$1" && pwd)" || exit 2
cd "$ROOT" || exit 1
set -a; . deploy/.env; set +a
export PYTHONIOENCODING=utf-8
JUDGE="${JUDGE_MODEL:-gpt-4.1}"
PY="uv run --extra graph --extra pdf python -X utf8"
cd ai || exit 1
echo "시작 $(date '+%F %T') · 코드 $(git rev-parse --short HEAD) · 실제 AI 를 부른다(한 번에 약 2달러)" | tee -a "$OUT/진행.txt"
step() {   # 이름, 명령… — 결과 파일이 있으면 건너뛴다
  local name="$1"; shift
  if [ -s "$OUT/$name.json" ]; then echo "[$name] 결과가 이미 있어 건너뜀" | tee -a "$OUT/진행.txt"; return 0; fi
  echo "[$name] 시작 $(date '+%T')" | tee -a "$OUT/진행.txt"
  "$@" --json "$OUT/$name.json" > "$OUT/$name.log" 2>&1
  local code=$?
  echo "[$name] 끝 $(date '+%T') · 종료 코드 $code" | tee -a "$OUT/진행.txt"
  return $code
}
step 1_검색 $PY ../eval/hit_at_k.py --rerank --record
step 2_답변점검 $PY ../eval/answer_checks.py --runs 2
AI_DB_URL="${AI_DB_URL%/*}/oom_eval" step 3_개정상태 $PY ../eval/answer_checks.py ../data/answer_checks/revisions/revised_v1.json --runs 2
step 4_답변품질 $PY ../eval/answer_quality.py --judge-model "$JUDGE" --record
# 배포용 파일을 다시 만들고 이 DB 에도 넣는다 — 측정 시각이 같아 위에서 기록한 행과 겹치지 않고, 건너뛴 단계의 결과는 여기서 들어간다
if [ -s "$OUT/1_검색.json" ] && [ -s "$OUT/4_답변품질.json" ]; then
  $PY ../eval/export_eval_runs.py --hit "$OUT/1_검색.json" --quality "$OUT/4_답변품질.json" --source "로컬 실측(코드 $(git rev-parse --short HEAD))" 2>&1 | tee -a "$OUT/진행.txt"
  (cd "$ROOT" && uv run --with "psycopg[binary]" python -X utf8 scripts/load_eval_runs.py) 2>&1 | tee -a "$OUT/진행.txt"
  echo "data/eval/eval_runs.json 이 바뀌었다 — 커밋하면 서버도 이 값을 받는다" | tee -a "$OUT/진행.txt"
else
  echo "검색 또는 답변 품질 결과가 없어 data/eval/eval_runs.json 은 그대로 둔다" | tee -a "$OUT/진행.txt"
fi
echo "끝 $(date '+%F %T')" | tee -a "$OUT/진행.txt"
