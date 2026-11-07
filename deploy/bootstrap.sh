#!/usr/bin/env bash
# 운영 박스의 첫 기동: postgres → api(기동 시 Flyway) → bootstrap 컨테이너(시드 · 규정 색인 · 사례 · 근거 연결 · 체인 기록) → ai · web · caddy.
# 사용: deploy/ 에서  bash bootstrap.sh [seed] [data]   (기본 둘 다. 각 단계 멱등 — 데이터만 다시 넣을 때는 bash bootstrap.sh data)
#       시연용 개정판: bash bootstrap.sh revision:retail-refund-2027-01   (data/policies/revisions/<slug>)
# 환경: ENV_FILE(기본 .env) · COMPOSE_FILES(기본 -f docker-compose.prod.yml) — smoke.sh 가 스모크 오버라이드로 부른다
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose ${COMPOSE_FILES:--f docker-compose.prod.yml} --env-file ${ENV_FILE:-.env}"
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

step "postgres · api (healthy 까지 대기)"
$C up -d --wait postgres api
step "bootstrap ${*:-seed data}"
$C --profile bootstrap run --rm bootstrap "$@"
step "ai(워커 내장) · web · caddy"
$C up -d ai web caddy
step "적재 결과"
$C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F " | "' <<'SQL'
SELECT 'policy_documents active', count(*) FROM policy_documents WHERE status = 'active'
UNION ALL SELECT 'policy_chunks (임베딩 없음)', count(*) FILTER (WHERE embedding IS NULL) FROM policy_chunks
UNION ALL SELECT 'policy_chunks', count(*) FROM policy_chunks
UNION ALL SELECT 'log_nodes', count(*) FROM log_nodes
UNION ALL SELECT 'log_nodes 정제 ' || refine_status, count(*) FROM log_nodes GROUP BY refine_status
UNION ALL SELECT 'log_nodes 근거 조문 연결', count(*) FROM log_nodes WHERE jsonb_typeof(attributes->'policy_refs') = 'array'
UNION ALL SELECT 'log_resolutions', count(*) FROM log_resolutions
UNION ALL SELECT 'log_edges 연결 사유', count(*) FROM log_edges WHERE reason IS NOT NULL;
SQL
echo; echo "done in $(( $(date +%s) - t0 ))s"
