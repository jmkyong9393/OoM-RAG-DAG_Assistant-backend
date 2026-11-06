#!/bin/sh
# 빈 운영 DB 를 시연 데이터로 채운다. bootstrap 컨테이너(AI 이미지) 안에서 돈다 — 호스트에서는 bootstrap.sh 로 부른다.
# 단계: seed(시드) · data(규정 색인 → 사례 적재 → 정제 → 근거 조문 · 체인 기록 연결). 전부 멱등이라 다시 돌려도 된다.
set -eu
STEPS="${*:-seed data}"
PY=/app/.venv/bin/python
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

# 원본은 읽기 전용 마운트 — load_logs 가 다시 쓰는 *.ids.json(노드 id 대응표)은 이 사본에만 남는다
rm -rf /work && mkdir -p /work && cp -r /src/scripts /src/data /work/
cd /work

for s in $STEPS; do
  case "$s" in
    seed)
      step "seed — 회사·부서·사용자 (DATABASE_URL, 관리자)"
      uv pip install -q --python "$PY" bcrypt     # 시드에만 필요해 이미지에는 넣지 않는다
      "$PY" scripts/seed.py > /tmp/seed.log || { cat /tmp/seed.log; exit 1; }   # sh 에는 pipefail 이 없어 tail 파이프로는 실패를 놓친다
      tail -2 /tmp/seed.log ;;
    data)
      step "규정 색인 — data/policies/*.chunks.json (AI_DB_URL, oom_ai)"
      "$PY" scripts/index_policy.py > /tmp/index.log || { cat /tmp/index.log; exit 1; }
      cat /tmp/index.log
      # 새로 색인했거나 용어가 늘면 --reindex(전체 재토크나이즈 — 그동안 어휘 검색이 빈다). 전부 skipped 면 빈 것만 채운다
      if grep -qE '^(indexed|reindexed|moved) |terms\+' /tmp/index.log; then R=--reindex; else R=; fi
      step "토큰 · 임베딩 백필 ${R:-(빈 것만)}"
      (cd /app && "$PY" -m app.worker --once $R)
      step "사례 적재 — POST /logs/bulk ($API_BASE)"
      "$PY" scripts/load_logs.py --base "$API_BASE"
      step "사례 정제 (대기 건만)"
      (cd /app && "$PY" -m app.worker --once)
      step "근거 조문 연결 · 체인 기록"
      "$PY" scripts/load_alignment.py
      "$PY" scripts/load_chain_records.py ;;
    *) echo "알 수 없는 단계: $s (seed | data)"; exit 2 ;;
  esac
done
step "끝"
