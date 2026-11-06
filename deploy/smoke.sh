#!/usr/bin/env bash
# 운영 compose 를 로컬에서 끝까지 돌려보는 스모크: postgres → flyway → bootstrap.sh(api → 데이터 적재 → ai·web(스텁)·caddy) → Caddy 경유로 health·로그인·로그 목록·SSE.
# 사용: deploy/ 에서  bash smoke.sh [--keep] [--quick]   (.env.smoke 필요 — .env.example 의 운영 절 참고)
#   --keep: 끝나고 내리지 않음 · --quick: 데이터는 시드만(규정 색인·사례 정제의 임베딩·LLM 호출 없이)
set -euo pipefail
cd "$(dirname "$0")"
KEEP=0; STEPS="seed data"
for a in "$@"; do case "$a" in --keep) KEEP=1;; --quick) STEPS="seed";; *) echo "알 수 없는 인자: $a"; exit 2;; esac; done
export COMPOSE_FILES="-f docker-compose.prod.yml -f docker-compose.smoke.yml" ENV_FILE=.env.smoke
C="docker compose $COMPOSE_FILES --env-file $ENV_FILE"
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

step "postgres"
$C up -d postgres
step "flyway migrate"
out=$($C --profile migrate run --rm flyway 2>&1) || { echo "$out" | tail -20; exit 1; }
echo "$out" | grep -E "Successfully|up to date"
step "bootstrap.sh $STEPS (api → 데이터 적재 → ai · web(stub) · caddy)"
bash bootstrap.sh $STEPS
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
echo "login: $code"   # 응답 원문은 이름·이메일이 있어 출력하지 않는다
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
docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep -E "oom-prod"
if [ "$KEEP" = 0 ]; then
  step "정리 (볼륨 포함)"
  $C down -v
  rm -f ./.smoke.jar ./.smoke-login.json ./.smoke-q.json
fi
echo; echo "done in $(( $(date +%s) - t0 ))s"
