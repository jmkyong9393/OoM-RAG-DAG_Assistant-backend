#!/usr/bin/env bash
# 직전 배포로 되돌린다 — 직전 설정(.env.prev: 이미지 태그)으로 롤링 교체한다. 서버에 남겨 둔 직전 이미지를 쓰므로 빌드 · 전송이 없다.
# 사용: 서버의 deploy/ 에서  bash rollback.sh   (환경: ENV_FILE · COMPOSE_FILES — bootstrap.sh 와 같다)
# 한계: DB 는 되돌리지 않는다. 되돌리는 배포에 DB 마이그레이션이 있었다면 backup.sh 의 배포 직전 백업으로 DB 를 복구해야 한다(아래에서 알려 준다).
#       레포 파일(Caddyfile · compose · 데이터)은 지금 커밋 그대로다 — 코드만 되돌린다.
set -euo pipefail
cd "$(dirname "$0")"
E=${ENV_FILE:-.env}   # 지금 설정. 직전 설정은 $E.prev(2-ship.sh 가 배포 때 남긴다)
[ -f "$E.prev" ] || { echo "$E.prev 가 없다 — 되돌릴 직전 배포가 없다"; exit 1; }
img() { grep "^$1=" "$2" | cut -d= -f2; }
for k in API_IMAGE AI_IMAGE WEB_IMAGE; do
  i=$(img $k "$E.prev"); docker image inspect "$i" >/dev/null 2>&1 || { echo "직전 이미지 $i 가 서버에 없다 — 그 커밋으로 2-ship.sh 를 다시 돌린다"; exit 1; }
  echo "  $k: $(img $k "$E") → $i"
done
old=$(img API_IMAGE "$E.prev"); new=$(img API_IMAGE "$E")
if git -C .. cat-file -e "${old##*:}^{commit}" 2>/dev/null && git -C .. cat-file -e "${new##*:}^{commit}" 2>/dev/null \
   && ! git -C .. diff --quiet "${old##*:}" "${new##*:}" -- api/src/main/resources/db/migration; then
  echo "  주의: 되돌리는 사이에 DB 마이그레이션이 바뀌었다 — 코드는 되돌리지만 DB 는 새 스키마 그대로다."
  echo "        문제가 계속되면 배포 직전 백업으로 복구: ls -1t ~/oom-backups/deploy-*.sql.gz | head -1"
  export RECREATE="api"
fi
cp "$E" "$E.rolledback" && cp "$E.prev" "$E"
bash rollout.sh ai api web
echo "되돌림 끝 — 되돌리기 전 설정은 $E.rolledback"
