#!/usr/bin/env bash
# 무중단 롤링 교체 — 서비스마다: 새 컨테이너를 옆에 띄운다 → healthy 까지 기다린다 → 주소가 퍼질 때까지 기다린다 → 옛 컨테이너를 정상 종료한다.
# 한 서비스씩 하므로 두 벌이 겹치는 것은 그 서비스 하나뿐이다. 설정 지문(config hash)이 그대로인 서비스는 건드리지 않는다.
# 사용: deploy/ 에서  bash rollout.sh ai api web
# 환경: RECREATE="api" — 옆에 띄우지 않고 정상 종료 뒤 다시 띄운다(DB 마이그레이션이 바뀐 배포: 옛 api 가 새 스키마로 돌지 않게)
#       FORCE="ai" — 설정 지문이 같아도 교체한다(DB 의 사용자 사전을 바꾼 뒤 ai 가 다시 읽게)
#       SETTLE(기본 5초) — 새 컨테이너가 healthy 가 된 뒤 Caddy · api 가 새 주소를 받을 때까지(둘 다 1초마다 다시 푼다)
#       ENV_FILE · COMPOSE_FILES — bootstrap.sh 와 같다
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose ${COMPOSE_FILES:--f docker-compose.prod.yml} --env-file ${ENV_FILE:-.env}"
SETTLE=${SETTLE:-5}; RECREATE=" ${RECREATE:-} "; FORCE=" ${FORCE:-} "   # FORCE="ai": 설정이 같아도 옆에 띄워 바꾼다(사전 · 캐시를 다시 읽힐 때)
t0=$(date +%s); say() { echo "  [$(( $(date +%s) - t0 ))s] $*"; }
state() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo gone; }

for svc in "$@"; do
  old=$($C ps -q "$svc")
  want=$($C config --hash "$svc" | awk '{print $2}')
  have=$([ -n "$old" ] && docker inspect -f '{{index .Config.Labels "com.docker.compose.config-hash"}}' $old | sort -u | head -1 || true)
  if [ -n "$old" ] && [ "$want" = "$have" ] && [[ "$FORCE" != *" $svc "* ]]; then say "$svc — 바뀐 것 없음"; continue; fi
  if [ -z "$old" ] || [[ "$RECREATE" == *" $svc "* ]] || [ "$(echo "$old" | wc -l)" -gt 1 ]; then
    say "$svc — 정상 종료 뒤 다시 띄움"
    $C up -d --no-deps --wait "$svc"
    continue
  fi
  say "$svc — 새 컨테이너를 옆에 띄움"
  $C up -d --no-deps --no-recreate --scale "$svc=2" "$svc"
  new=$($C ps -q "$svc" | grep -vxF "$old" | head -1)
  for i in $(seq 1 90); do s=$(state "$new"); [ "$s" = healthy ] && break; [ "$s" = unhealthy ] || [ "$s" = exited ] && break; sleep 2; done
  if [ "$(state "$new")" != healthy ]; then
    say "$svc — 새 컨테이너가 healthy 가 아니다($(state "$new")) — 새 것을 지우고 옛 것을 그대로 둔다"
    docker logs --tail 30 "$new" 2>&1 | sed 's/^/    /'
    docker rm -f "$new" >/dev/null
    $C up -d --no-deps --no-recreate --scale "$svc=1" "$svc"
    exit 1
  fi
  say "$svc — 새 컨테이너 healthy, ${SETTLE}초 뒤 옛 컨테이너 정상 종료"
  sleep "$SETTLE"
  docker stop -t 35 "$old" >/dev/null   # 정상 종료: 새 요청은 받지 않고 진행 중인 것(채팅 스트림 포함)을 끝낸다
  docker rm "$old" >/dev/null
  $C up -d --no-deps --no-recreate --scale "$svc=1" "$svc" >/dev/null 2>&1
  say "$svc — 교체 끝"
done
