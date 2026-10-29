#!/usr/bin/env bash
# 운영 compose 를 로컬에서 끝까지 돌려보는 스모크: postgres → flyway → 시드 → api·ai·worker·web(스텁)·caddy → Caddy 경유로 health·로그인·로그 목록·SSE.
# 사용: deploy/ 에서  bash smoke.sh [--keep]   (.env.smoke 필요 — .env.example 의 운영 절 참고. --keep 이면 끝나고 내리지 않음)
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose -f docker-compose.prod.yml -f docker-compose.smoke.yml --env-file .env.smoke"
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

step "postgres"
$C up -d postgres
step "flyway migrate"
$C --profile migrate run --rm flyway | grep -E "Successfully|ERROR" || true
step "seed (host → :15433)"
( cd .. && DATABASE_URL="postgresql://$(grep ^POSTGRES_USER= deploy/.env.smoke | cut -d= -f2):$(grep ^POSTGRES_PASSWORD= deploy/.env.smoke | cut -d= -f2)@localhost:15433/$(grep ^POSTGRES_DB= deploy/.env.smoke | cut -d= -f2)" \
  SEED_PASSWORD="$(grep ^SEED_PASSWORD= deploy/.env.smoke | cut -d= -f2)" uv run --with "psycopg[binary]" --with bcrypt scripts/seed.py | tail -2 )
step "api · ai · worker · web(stub) · caddy"
$C up -d api ai worker web caddy
step "health 대기"
for i in $(seq 1 60); do
  s=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/api/v1/actuator/health || true)
  [ "$s" = "200" ] && break; sleep 2
done
echo "actuator/health via caddy: $s"
curl -s http://localhost/api/v1/actuator/health; echo
step "로그인 (Caddy 경유, 쿠키 저장)"
rm -f ./.smoke.jar
code=$(curl -s -o ./.smoke-login.json -w "%{http_code}" -c ./.smoke.jar -H "Content-Type: application/json" \
  -d "{\"employee_no\":\"OOM0002\",\"password\":\"$(grep ^SEED_PASSWORD= .env.smoke | cut -d= -f2)\"}" http://localhost/api/v1/auth/login)
echo "login: $code  $(head -c 160 ./.smoke-login.json)"
grep -c OOMSESSION ./.smoke.jar | sed 's/^/OOMSESSION cookies: /'
step "로그 목록 (세션 쿠키 + X-Workspace-Id)"
curl -s -b ./.smoke.jar -H "X-Workspace-Id: 1" "http://localhost/api/v1/logs?type=all" | head -c 200; echo
step "SSE /chat (Caddy 경유, 첫 이벤트까지 시간)"
ts=$(date +%s%N)
printf '{"query":"VPN 접속 시 E-403 오류가 계속 나는데 예전에 어떻게 해결했어?"}' > ./.smoke-q.json   # 한글은 파일로 — Git Bash curl -d 는 인코딩이 깨져 400
curl -sN -b ./.smoke.jar -H "X-Workspace-Id: 1" -H "Content-Type: application/json; charset=utf-8" -H "Accept: text/event-stream" \
  --data-binary @./.smoke-q.json http://localhost/api/v1/chat \
  | while IFS= read -r line; do
      case "$line" in
        event:token) tok=$(( ${tok:-0} + 1 )); [ "$tok" = 1 ] && echo "  +$(( ($(date +%s%N) - ts) / 1000000 ))ms  event:token (first)";;
        event:*) echo "  +$(( ($(date +%s%N) - ts) / 1000000 ))ms  $line";;
      esac
      [ "$line" = "event:done" ] && break
    done
step "web 스텁 (/)"
curl -s http://localhost/ | head -c 80; echo
step "컨테이너 메모리"
docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep -E "deploy-|oom-prod"
if [ "${1:-}" != "--keep" ]; then
  step "정리 (볼륨 포함)"
  $C down -v
  rm -f ./.smoke.jar ./.smoke-login.json ./.smoke-q.json
fi
echo; echo "done in $(( $(date +%s) - t0 ))s"
