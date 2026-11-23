#!/usr/bin/env bash
# DB 와 업로드 원본을 백업하고 종류마다 최근 N개만 남긴다(날짜가 아니라 개수 — 서버가 며칠 꺼져 있어도 마지막 N개는 남는다).
# 사용: deploy/ 에서  bash backup.sh daily   (매일 — 7개)   |   bash backup.sh deploy   (배포 직전 — 3개)
# 환경: BACKUP_DIR(기본 ~/oom-backups) · ENV_FILE · COMPOSE_FILES(bootstrap.sh 와 같다)
# 복구: gunzip -c <파일>.sql.gz | docker exec -i oom-prod-postgres psql -U oom -d oom   (빈 DB 에. 업로드는 <파일>-uploads.tar.gz 를 볼륨에 풀기)
# 한계: 같은 서버 디스크에 남는다 — 서버가 사라지면 함께 사라진다(시연 전 Lightsail 스냅샷 1회로 보완)
set -euo pipefail
cd "$(dirname "$0")"
KIND=${1:?daily | deploy}
case "$KIND" in daily) KEEP=7;; deploy) KEEP=3;; *) echo "알 수 없는 종류: $KIND"; exit 2;; esac
C="docker compose ${COMPOSE_FILES:--f docker-compose.prod.yml} --env-file ${ENV_FILE:-.env}"
DIR=${BACKUP_DIR:-$HOME/oom-backups}
umask 077; mkdir -p "$DIR"; chmod 700 "$DIR"
free_kb=$(df -Pk "$DIR" | awk 'NR == 2 {print $4}')
if [ "$free_kb" -lt 1048576 ]; then echo "[backup] 남은 공간 1GB 미만 — 이번 백업은 건너뛴다(옛 백업은 그대로)"; exit 0; fi

name="$KIND-$(date +%Y%m%d-%H%M%S)"
# 임시 파일에 쓰고, 둘 다 성공하면 그때 최종 이름으로 — 실패한 백업이 개수를 차지하거나 멀쩡한 옛 백업을 밀어내지 않게
$C exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip -c > "$DIR/.$name.sql.gz.part"
vol=$(docker volume ls -q --filter label=com.docker.compose.project=oom-prod --filter label=com.docker.compose.volume=oom-prod-uploads | head -1)
if [ -n "$vol" ]; then
  docker run --rm --entrypoint tar -v "$vol:/u:ro" caddy:2 czf - -C /u . > "$DIR/.$name-uploads.tar.gz.part"   # 이미 있는 caddy 이미지의 tar — 이미지를 더 받지 않는다
  mv "$DIR/.$name-uploads.tar.gz.part" "$DIR/$name-uploads.tar.gz"
fi
mv "$DIR/.$name.sql.gz.part" "$DIR/$name.sql.gz"
chmod 600 "$DIR/$name"*
echo "[backup] $DIR/$name.sql.gz ($(du -h "$DIR/$name.sql.gz" | cut -f1))"

# 성공한 뒤에만: 같은 종류에서 최근 KEEP 개를 넘는 것을 지운다
ls -1t "$DIR/$KIND"-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  rm -f "$f" "${f%.sql.gz}-uploads.tar.gz"; echo "[backup] 지움 $(basename "$f")"
done
rm -f "$DIR"/.*.part 2>/dev/null || true   # 앞서 실패해 남은 임시 파일
