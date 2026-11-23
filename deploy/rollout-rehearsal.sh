#!/usr/bin/env bash
# 무중단 배포 리허설: 떠 있는 운영 구성에 요청을 계속 보내면서 갱신 배포(bootstrap.sh → rollout.sh)를 하고, 실패한 요청 수를 센다.
# 준비: deploy/ 에서  WEB_IMAGE=<프론트 이미지> bash smoke.sh --web --quick --keep   (구성을 띄워 둔다)
# 사용: deploy/ 에서  WEB_IMAGE=<같은 프론트 이미지> bash rollout-rehearsal.sh <결과 폴더>
#   - 지금 이미지에 새 태그(:next-<시각>)를 붙여 "새 버전"으로 배포한다 — 내용은 같고 설정 지문이 바뀌어 api · ai · web 이 모두 교체된다
#   - 요청: health(Caddy → api) · /login(→ web) 0.2초마다, 로그 목록(→ api → DB) 0.5초마다, 채팅(→ api → ai, 실제 답변) 3초마다
#   - 채팅은 실제 AI 를 부른다(질문 수십 건, 소액)
set -uo pipefail
export MSYS_NO_PATHCONV=1   # Windows Git Bash 가 docker 의 /u 같은 경로를 바꾸지 않게(리눅스에서는 영향 없음)
cd "$(dirname "$0")"
OUT=${1:?결과 폴더}; mkdir -p "$OUT"
export COMPOSE_FILES="-f docker-compose.prod.yml -f docker-compose.rehearsal.yml" ENV_FILE=.env.smoke
C="docker compose $COMPOSE_FILES --env-file $ENV_FILE"
B=http://localhost
ms() { echo $(( $(date +%s%N) / 1000000 )); }

curl -s -o /dev/null -c "$OUT/.jar" -H "Content-Type: application/json" \
  -d "{\"employee_no\":\"OOM0002\",\"password\":\"$(grep ^SEED_PASSWORD= .env.smoke | cut -d= -f2)\"}" $B/api/v1/auth/login
printf '{"query":"VPN 접속 시 E-403 오류가 계속 나는데 예전에 어떻게 해결했어?"}' > "$OUT/.q.json"

probe() {   # 이름 간격 curl-인자…  → 줄마다 "시각ms 상태코드 걸린초"
  local name=$1 gap=$2; shift 2
  while [ ! -f "$OUT/.stop" ]; do echo "$(ms) $(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 70 "$@")" >> "$OUT/$name.txt"; sleep "$gap"; done
}
chat() {   # 3초마다 채팅 하나를 띄우고, 끝나면 "시작ms 결과(done|error|cut) 걸린ms"
  local n=0
  while [ ! -f "$OUT/.stop" ]; do
    n=$((n + 1))
    ( s=$(ms); r=$(curl -sN --max-time 120 -b "$OUT/.jar" -H "X-Workspace-Id: 1" -H "Content-Type: application/json; charset=utf-8" \
        -H "Accept: text/event-stream" --data-binary @"$OUT/.q.json" $B/api/v1/chat | grep -E '^event:(done|error)' | head -1)
      case "$r" in event:done) k=done;; event:error) k=error;; *) k=cut;; esac
      echo "$s $k $(( $(ms) - s ))" >> "$OUT/chat.txt" ) &
    sleep 3
  done
  wait
}

mem() {   # 2초마다 이 구성의 컨테이너 메모리 합(MiB)
  while [ ! -f "$OUT/.stop" ]; do
    docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | awk -v t="$(ms)" '/oom-prod/ {v=$2; u=v; sub(/[0-9.]+/, "", u); sub(/[A-Za-z]+/, "", v); m=(u=="GiB")?v*1024:(u=="KiB")?v/1024:v; s+=m; n++} END {printf "%s %.0f %d\n", t, s, n}' >> "$OUT/mem.txt"
    sleep 2
  done
}

rm -f "$OUT"/*.txt "$OUT/.stop"
NEXT=next-$(date +%s)   # 실행마다 다른 태그 — 같은 태그면 설정 지문이 그대로라 교체가 일어나지 않는다
for k in api ai; do docker tag "$(docker inspect -f '{{.Config.Image}}' "$($C ps -q $k | head -1)")" "oom-$k:$NEXT"; done
docker tag "$WEB_IMAGE" "oom-web:$NEXT"

probe health 0.2 $B/api/v1/actuator/health & probe login 0.2 -L $B/login & probe logs 0.5 -b "$OUT/.jar" -H "X-Workspace-Id: 1" "$B/api/v1/logs?type=all" &
chat & mem &
sleep 10   # 배포 전 기준 구간
echo "== 배포 시작 $(ms)" | tee "$OUT/deploy.log"
API_IMAGE=oom-api:$NEXT AI_IMAGE=oom-ai:$NEXT WEB_IMAGE=oom-web:$NEXT MIGRATION_CHECK=skip BACKUP_DIR="$OUT/backups" \
  bash bootstrap.sh eval >> "$OUT/deploy.log" 2>&1; echo "== 배포 끝 $(ms) (종료 코드 $?)" | tee -a "$OUT/deploy.log"
sleep 15   # 배포 뒤 구간 — 마지막 채팅이 끝날 시간
touch "$OUT/.stop"; wait

echo; echo "== 결과 (배포 중 포함 전체)"
for f in health login logs; do
  awk -v n=$f '{t++; if ($2 !~ /^2/) {bad++; codes[$2]++} if ($3 > mx) mx = $3} END {printf "  %-7s 요청 %d · 실패 %d", n, t, bad + 0; for (c in codes) printf " [%s ×%d]", c, codes[c]; printf " · 최대 %.2f초\n", mx}' "$OUT/$f.txt"
done
awk '{t++; c[$2]++; if ($3 > mx) mx = $3} END {printf "  chat    시작 %d · 끝까지 %d · 오류 %d · 끊김 %d · 최대 %.1f초\n", t, c["done"] + 0, c["error"] + 0, c["cut"] + 0, mx / 1000}' "$OUT/chat.txt"
awk '{if ($2 > mx) {mx = $2; n = $3}} END {printf "  메모리  이 구성 컨테이너 합 최대 %d MiB (그때 컨테이너 %d개)\n", mx, n}' "$OUT/mem.txt"
docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep oom-prod
