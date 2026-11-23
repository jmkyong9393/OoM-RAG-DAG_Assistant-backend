#!/usr/bin/env bash
# 운영 compose 를 로컬에서 끝까지 돌려보는 스모크: 이미지 빌드 → postgres → flyway → bootstrap.sh(api → 데이터 적재 → ai·web(스텁)·caddy) → Caddy 경유로 health·로그인·로그 목록·SSE.
# 사용: deploy/ 에서  bash smoke.sh [--keep] [--quick]   (.env.smoke 필요 — .env.example 의 운영 절 참고)
#   --keep: 끝나고 내리지 않음 · --quick: 데이터는 시드 + 평가 결과만(규정 색인·사례 정제의 임베딩·LLM 호출 없이)
#   --web: web 스텁 대신 실제 프론트 이미지로(배포 전 리허설). 이미지는 환경변수 WEB_IMAGE 로 준다
set -euo pipefail
cd "$(dirname "$0")"
KEEP=0; STEPS="seed data eval"; OVERRIDE=docker-compose.smoke.yml
for a in "$@"; do case "$a" in --keep) KEEP=1;; --quick) STEPS="seed eval";; --web) OVERRIDE=docker-compose.rehearsal.yml;; *) echo "알 수 없는 인자: $a"; exit 2;; esac; done
export COMPOSE_FILES="-f docker-compose.prod.yml -f $OVERRIDE" ENV_FILE=.env.smoke
C="docker compose $COMPOSE_FILES --env-file $ENV_FILE"
t0=$(date +%s)
step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

step "이미지 빌드 (api · ai — 지금 작업 트리. 남아 있던 옛 이미지로 검사하지 않게)"
$C build api ai
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
step "처리방침 문안 · 판 (동의 화면이 읽는다 — 문안 마운트가 빠지면 404, 판이 없으면 재동의가 동작하지 않는다)"
v=$(curl -s -D - -o /dev/null http://localhost/api/v1/auth/privacy-policy | tr -d '\r' | awk -F': ' 'tolower($1)=="x-policy-version"{print $2}')
[ -n "$v" ] || { echo "X-Policy-Version 없음 — PRIVACY_POLICY_PATH 마운트와 문안 머리의 '판:' 확인"; exit 1; }
echo "privacy-policy 판: $v"
step "로그인 (Caddy 경유, 쿠키 저장)"
rm -f ./.smoke.jar
code=$(curl -s -o ./.smoke-login.json -w "%{http_code}" -c ./.smoke.jar -H "Content-Type: application/json" \
  -d "{\"employee_no\":\"OOM0002\",\"password\":\"$(grep ^SEED_PASSWORD= .env.smoke | cut -d= -f2)\"}" http://localhost/api/v1/auth/login)
echo "login: $code"   # 응답 원문은 이름·이메일이 있어 출력하지 않는다
grep -c OOMSESSION ./.smoke.jar | sed 's/^/OOMSESSION cookies: /'
step "로그 목록 (세션 쿠키 + X-Workspace-Id)"
curl -s -b ./.smoke.jar -H "X-Workspace-Id: 1" "http://localhost/api/v1/logs?type=all" | head -c 200; echo
step "대시보드 지표 (eval 단계가 넣은 평가 결과)"
curl -s -b ./.smoke.jar -H "X-Workspace-Id: 1" http://localhost/api/v1/dashboard/summary | grep -oE '"(hit_at_5|faithfulness|answer_relevance)":[0-9.]+|"is_sample":(true|false)' | tr '\n' ' '; echo
step "SSE /chat (Caddy 경유, 첫 이벤트까지 시간)"
ts=$(date +%s%N)
printf '{"query":"VPN 접속 시 E-403 오류가 계속 나는데 예전에 어떻게 해결했어?"}' > ./.smoke-q.json   # 한글은 파일로 — Git Bash curl -d 는 인코딩이 깨져 400
curl -sN -b ./.smoke.jar -H "X-Workspace-Id: 1" -H "Content-Type: application/json; charset=utf-8" -H "Accept: text/event-stream" \
  --data-binary @./.smoke-q.json http://localhost/api/v1/chat \
  | { while IFS= read -r line; do
      case "$line" in
        event:token) tok=$(( ${tok:-0} + 1 )); [ "$tok" = 1 ] && echo "  +$(( ($(date +%s%N) - ts) / 1000000 ))ms  event:token (first)";;
        event:*) echo "  +$(( ($(date +%s%N) - ts) / 1000000 ))ms  $line";;
      esac
      if [ "$line" = "event:done" ]; then break; fi
    done; } | tee ./.smoke-sse.txt
grep -q "event:done" ./.smoke-sse.txt || { echo "SSE 가 done 없이 끝났다 (api·ai 로그 확인)"; exit 1; }
step "web (/) — 스텁이면 한 줄, 실제 프론트면 대문 · 로그인 화면의 상태 코드"
if [ "$OVERRIDE" = docker-compose.smoke.yml ]; then curl -s http://localhost/ | head -c 80; echo
else for p in / /login; do c=$(curl -s -o /dev/null -w "%{http_code}" -L "http://localhost$p"); echo "  GET $p → $c"; [ "$c" = 200 ] || { echo "프론트가 $p 에 200 을 주지 않는다"; exit 1; }; done; fi
step "컨테이너 메모리"
docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep -E "oom-prod"
if [ "$KEEP" = 0 ]; then
  step "정리 (볼륨 포함)"
  $C down -v
  rm -f ./.smoke.jar ./.smoke-login.json ./.smoke-q.json ./.smoke-sse.txt
fi
echo; echo "done in $(( $(date +%s) - t0 ))s"
