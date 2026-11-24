#!/usr/bin/env bash
# 운영 박스의 첫 기동: postgres → api(기동 시 Flyway) → bootstrap 컨테이너(시드 · 규정 색인 · 사례 · 근거 연결 · 체인 기록 · 평가 결과) → ai · web · caddy.
# 이미 떠 있으면 갱신 배포: 배포 직전 백업 → 롤링 교체(rollout.sh, 멈추지 않고) → 진입점 설정 다시 읽기 → 데이터 단계.
# 사용: deploy/ 에서  bash bootstrap.sh [seed] [data] [eval]   (기본 셋 다. 각 단계 멱등 — 데이터만 다시 넣을 때는 bash bootstrap.sh data)
#       시연용 개정판: bash bootstrap.sh revision:retail-refund-2027-01   (data/policies/revisions/<slug>)
# 환경: ENV_FILE(기본 .env) · COMPOSE_FILES(기본 -f docker-compose.prod.yml) — smoke.sh 가 스모크 오버라이드로 부른다
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose ${COMPOSE_FILES:--f docker-compose.prod.yml} --env-file ${ENV_FILE:-.env}"
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

if [ -z "$($C ps -q api 2>/dev/null)" ]; then
  # 첫 기동(또는 내려가 있던 서버): 순서대로 띄운다
  step "postgres · api (healthy 까지 대기)"
  $C up -d --wait postgres api
  step "bootstrap ${*:-seed data eval}"
  $C --profile bootstrap run --rm bootstrap "$@"
  step "ai(워커 내장) · web · caddy (healthy 까지 대기 — 뜨는 중인 ai 로 첫 질문이 가지 않게)"
  $C up -d --wait ai web caddy
else
  # 갱신 배포: 떠 있는 서비스를 멈추지 않고 바꾼다
  step "배포 직전 백업 (최근 3개 보관)"
  bash backup.sh deploy
  # 마이그레이션이 바뀌었으면 api 는 옆에 띄우지 않는다 — 옛 api 가 새 스키마로 도는 순간이 없게(정상 종료 뒤 다시 띄움).
  # 함수 파일(R__functions — CREATE OR REPLACE 라 이름 · 반환 형식이 그대로)만 바뀌었으면 옛 api 와 함께 돌아도 되므로 롤링으로 바꾼다.
  # 뷰 파일(R__views)은 지우고 다시 만들어 열이 빠질 수 있어 스키마와 같이 다룬다
  old_tag=$(docker inspect -f '{{.Config.Image}}' "$($C ps -q api | head -1)"); old_tag=${old_tag##*:}
  if git -C .. cat-file -e "$old_tag^{commit}" 2>/dev/null; then
    git -C .. diff --name-only "$old_tag" HEAD -- api/src/main/resources/db/migration | grep -qv '/R__functions\.sql$' && export RECREATE="api ${RECREATE:-}"
  elif [ "${MIGRATION_CHECK:-on}" != skip ]; then
    echo "  지금 api 이미지($old_tag)의 커밋을 레포에서 찾지 못해 마이그레이션 변경을 알 수 없다 — 안전하게 api 는 다시 띄운다"
    export RECREATE="api ${RECREATE:-}"
  fi
  step "롤링 교체 — ai → api → web${RECREATE:+ (다시 띄움: $RECREATE)}"
  bash rollout.sh ai api web
  # 진입점: 설정 파일만 바뀌었으면 끊김 없이 다시 읽고, compose 설정이 바뀌었으면 다시 띄운다(짧게 끊긴다)
  cid=$($C ps -q caddy)
  if [ -n "$cid" ] && [ "$($C config --hash caddy | awk '{print $2}')" = "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$cid")" ]; then
    $C exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  else
    $C up -d --no-deps --wait caddy
  fi
  step "bootstrap ${*:-seed data eval}"
  $C --profile bootstrap run --rm --no-deps bootstrap "$@"
fi
step "적재 결과"
$C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F " | "' <<'SQL'
SELECT 'policy_documents active', count(*) FROM policy_documents WHERE status = 'active'
UNION ALL SELECT 'policy_chunks (임베딩 없음)', count(*) FILTER (WHERE embedding IS NULL) FROM policy_chunks
UNION ALL SELECT 'policy_chunks', count(*) FROM policy_chunks
UNION ALL SELECT 'log_nodes', count(*) FROM log_nodes
UNION ALL SELECT 'log_nodes 정제 ' || refine_status, count(*) FROM log_nodes GROUP BY refine_status
UNION ALL SELECT 'log_nodes 근거 조문 연결', count(*) FROM log_nodes WHERE jsonb_typeof(attributes->'policy_refs') = 'array'
UNION ALL SELECT 'log_resolutions', count(*) FROM log_resolutions
UNION ALL SELECT 'log_edges 연결 사유', count(*) FROM log_edges WHERE reason IS NOT NULL
UNION ALL SELECT 'eval_runs (실측)', count(*) FROM eval_runs WHERE NOT is_sample;
SQL
echo; echo "done in $(( $(date +%s) - t0 ))s"
