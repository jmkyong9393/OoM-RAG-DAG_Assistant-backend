#!/usr/bin/env bash
# 개발 PC 에서 이미지를 빌드해 서버로 보내고 띄운다(레지스트리 없이 docker save → ssh → docker load). 다시 돌리면 갱신 배포다 — 모든 단계 멱등.
# 사용: bash deploy/lightsail/2-ship.sh <서버 IP> [bootstrap.sh 인자…]   (서버가 이미 떠 있으면 무중단 롤링 교체 — bootstrap.sh)     예) … 1.2.3.4            첫 배포(시드 · 규정 · 사례 · 평가 결과 적재)
#                                                                             … 1.2.3.4 seed eval  데이터 재색인 없이
# 준비: Docker 실행 중, deploy/.env.prod(make-env.sh), 백엔드 작업 트리가 깨끗하고 푸시돼 있을 것(서버는 같은 커밋을 GitHub 에서 받는다)
# 환경: KEY(기본 ~/.ssh/oom-assist.pem) · FRONT_DIR(프론트 레포, 기본 ../OoM-RAG-DAG_Assistant) · FRONT_REF(기본 origin/main) · SSH_USER(기본 ubuntu)
set -euo pipefail
cd "$(dirname "$0")/../.."
IP=${1:?서버 IP}; shift
KEY=${KEY:-$HOME/.ssh/oom-assist.pem}; SSH_USER=${SSH_USER:-ubuntu}
FRONT_DIR=${FRONT_DIR:-../OoM-RAG-DAG_Assistant}; FRONT_REF=${FRONT_REF:-origin/main}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 $SSH_USER@$IP"
t0=$(date +%s); step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

[ -f deploy/.env.prod ] || { echo "deploy/.env.prod 가 없습니다 — bash deploy/lightsail/make-env.sh $IP"; exit 1; }
[ -f "$KEY" ] || { echo "SSH 키가 없습니다: $KEY"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "커밋하지 않은 변경이 있습니다 — 서버는 GitHub 의 커밋을 받으므로 먼저 커밋 · 푸시하세요"; git status --short; exit 1; }
SHA=$(git rev-parse --short HEAD)
git fetch -q origin
[ -n "$(git branch -r --contains HEAD)" ] || { echo "커밋 $SHA 가 원격에 없습니다 — 먼저 푸시하세요"; exit 1; }
REPO_URL=$(git remote get-url origin)

# 이미지 태그 = 그 이미지에 들어가는 경로를 마지막으로 바꾼 커밋(deploy/image-tags.sh — 자동 배포와 같은 규칙).
# 서버 레포는 늘 $SHA — 데이터 적재 · 마운트 파일은 레포에서, 앱 코드 · 마이그레이션은 이미지에서 온다(경로가 같으면 내용도 같다)
eval "$(bash deploy/image-tags.sh HEAD)"; ATAG=$API_TAG; ITAG=$AI_TAG
build() { docker image inspect "$1" >/dev/null 2>&1 && echo "  $1 — 이미 있음, 건너뜀" || docker build -q --label oom.project=assist -t "$@"; }   # 표식: trim-images.sh 가 이 프로젝트 것만 지운다
step "이미지 빌드 — api ($ATAG) · ai ($ITAG), 레포 $SHA"
build "oom-api:$ATAG" api
build "oom-ai:$ITAG" -f ai/Dockerfile .
step "이미지 빌드 — web ($FRONT_REF, 프론트 레포는 읽기만)"
git -C "$FRONT_DIR" fetch -q origin
FSHA=$(git -C "$FRONT_DIR" rev-parse --short "$FRONT_REF")
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
docker image inspect "oom-web:$FSHA" >/dev/null 2>&1 || git -C "$FRONT_DIR" archive "$FRONT_REF" | tar -x -C "$TMP"
build "oom-web:$FSHA" "$TMP"

step "서버 준비 대기 (첫 부팅의 Docker 설치)"
for i in $(seq 1 60); do $SSH 'test -f /var/lib/oom-user-data.done && docker version >/dev/null 2>&1' 2>/dev/null && break; sleep 10; done
$SSH 'docker version --format "docker {{.Server.Version}}" && free -m | sed -n 2,3p'

step "이미지 전송 (서버에 없는 것만, 압축해서 ssh 로 — 수 분)"
SEND=""; for img in "oom-api:$ATAG" "oom-ai:$ITAG" "oom-web:$FSHA"; do
  $SSH "docker image inspect $img >/dev/null 2>&1" && echo "  $img — 서버에 있음, 건너뜀" || SEND="$SEND $img"
done
# gzip -1: 기본 압축과 크기 차이는 작고 압축이 훨씬 빠르다
[ -z "$SEND" ] || docker save $SEND | gzip -1 | $SSH 'gunzip | docker load'

step "자동 배포 잠시 멈춤 (이 배포가 끝나면 다시 켠다)"
$SSH 'mkdir -p ~/oom-deploy && touch ~/oom-deploy/pause'
trap 'rm -rf "$TMP"; $SSH "rm -f ~/oom-deploy/pause" 2>/dev/null || true' EXIT

step "서버의 레포를 같은 커밋으로 ($SHA)"
$SSH "set -e; [ -d oom/.git ] || git clone -q '$REPO_URL' oom; cd oom && git fetch -q origin && git checkout -q --detach $SHA && git log --oneline -1"

step "운영 설정 올리기 (deploy/.env, 이미지 태그 반영)"
sed -e "s|^API_IMAGE=.*|API_IMAGE=oom-api:$ATAG|" -e "s|^AI_IMAGE=.*|AI_IMAGE=oom-ai:$ITAG|" -e "s|^WEB_IMAGE=.*|WEB_IMAGE=oom-web:$FSHA|" deploy/.env.prod \
  | $SSH 'umask 077; [ -f oom/deploy/.env ] && cp oom/deploy/.env oom/deploy/.env.prev; cat > oom/deploy/.env'   # .env.prev: 직전 배포로 되돌릴 때(rollback.sh)

step "기동 — bootstrap.sh ${*:-(seed data eval)}"
$SSH "cd oom/deploy && bash bootstrap.sh $*"

step "서버 시간대 한국 시간으로 (이미 만든 서버에도 — 다시 돌려도 같다)"
$SSH 'sudo ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime && echo Asia/Seoul | sudo tee /etc/timezone >/dev/null && sudo systemctl restart cron && date "+  %Y-%m-%d %H:%M %Z"'
step "매일 백업 예약 (한국 시간 새벽 3시 30분, 최근 7개 보관 — deploy/backup.sh)"
$SSH 'echo "30 3 * * * $USER mkdir -p \$HOME/oom-backups && cd \$HOME/oom/deploy && bash backup.sh daily >> \$HOME/oom-backups/backup.log 2>&1" | sudo tee /etc/cron.d/oom-backup >/dev/null && sudo chmod 644 /etc/cron.d/oom-backup && echo "  /etc/cron.d/oom-backup"'

step "자동 배포 예약 (2분마다 새 버전 확인 — deploy/autodeploy.sh, 기록 ~/oom-deploy/auto.log)"
$SSH "echo '$SHA $ATAG $ITAG $FSHA' > ~/oom-deploy/deployed"   # 지금 배포한 조합 — 자동 배포가 같은 조합을 다시 배포하지 않게
$SSH 'echo "*/2 * * * * $USER cd \$HOME/oom && bash deploy/autodeploy.sh" | sudo tee /etc/cron.d/oom-autodeploy >/dev/null && sudo chmod 644 /etc/cron.d/oom-autodeploy && echo "  /etc/cron.d/oom-autodeploy"'

step "옛 이미지 정리 — 서버 (종류마다 지금 + 직전 하나)"
$SSH "cd oom && bash deploy/trim-images.sh api=$ATAG ai=$ITAG web=$FSHA; df -h / | tail -1"

step "확인"
ORIGIN=$(grep ^SITE_ORIGIN= deploy/.env.prod | cut -d= -f2)
for i in $(seq 1 30); do s=$(curl -ks -o /dev/null -w '%{http_code}' "$ORIGIN/api/v1/actuator/health" || true); [ "$s" = 200 ] && break; sleep 5; done
echo "health $s · 대문 $(curl -ks -o /dev/null -w '%{http_code}' "$ORIGIN/")"
$SSH 'docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep oom-prod; free -m | sed -n 2,3p'
step "옛 이미지 정리 — 이 PC (종류마다 지금 + 직전 하나)"
bash deploy/trim-images.sh api=$ATAG ai=$ITAG web=$FSHA
echo; echo "주소: $ORIGIN   (레포 $SHA · api $ATAG · ai $ITAG · web $FSHA, $(( $(date +%s) - t0 ))s)"
