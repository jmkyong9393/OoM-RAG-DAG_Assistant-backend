#!/usr/bin/env bash
# 개발 PC 에서 이미지를 빌드해 서버로 보내고 띄운다(레지스트리 없이 docker save → ssh → docker load). 다시 돌리면 갱신 배포다 — 모든 단계 멱등.
# 사용: bash deploy/lightsail/2-ship.sh <서버 IP> [bootstrap.sh 인자…]     예) … 1.2.3.4            첫 배포(시드 · 규정 · 사례 · 평가 결과 적재)
#                                                                             … 1.2.3.4 seed eval  데이터 재색인 없이
# 준비: Docker 실행 중, deploy/.env.prod(make-env.sh), 백엔드 작업 트리가 깨끗하고 푸시돼 있을 것(서버는 같은 커밋을 GitHub 에서 받는다)
# 환경: KEY(기본 ~/.ssh/oom-assist.pem) · FRONT_DIR(프론트 레포, 기본 ../OoM-RAG-DAG_Assistant) · FRONT_REF(기본 origin/fronend) · SSH_USER(기본 ubuntu)
set -euo pipefail
cd "$(dirname "$0")/../.."
IP=${1:?서버 IP}; shift
KEY=${KEY:-$HOME/.ssh/oom-assist.pem}; SSH_USER=${SSH_USER:-ubuntu}
FRONT_DIR=${FRONT_DIR:-../OoM-RAG-DAG_Assistant}; FRONT_REF=${FRONT_REF:-origin/fronend}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 $SSH_USER@$IP"
t0=$(date +%s); step() { echo; echo "== [$(( $(date +%s) - t0 ))s] $*"; }

[ -f deploy/.env.prod ] || { echo "deploy/.env.prod 가 없습니다 — bash deploy/lightsail/make-env.sh $IP"; exit 1; }
[ -f "$KEY" ] || { echo "SSH 키가 없습니다: $KEY"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "커밋하지 않은 변경이 있습니다 — 서버는 GitHub 의 커밋을 받으므로 먼저 커밋 · 푸시하세요"; git status --short; exit 1; }
SHA=$(git rev-parse --short HEAD)
git fetch -q origin
[ -n "$(git branch -r --contains HEAD)" ] || { echo "커밋 $SHA 가 원격에 없습니다 — 먼저 푸시하세요"; exit 1; }
REPO_URL=$(git remote get-url origin)

step "이미지 빌드 — api · ai ($SHA)"
docker build -q -t "oom-api:$SHA" api
docker build -q -f ai/Dockerfile -t "oom-ai:$SHA" .
step "이미지 빌드 — web ($FRONT_REF, 프론트 레포는 읽기만)"
git -C "$FRONT_DIR" fetch -q origin
FSHA=$(git -C "$FRONT_DIR" rev-parse --short "$FRONT_REF")
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git -C "$FRONT_DIR" archive "$FRONT_REF" | tar -x -C "$TMP"
docker build -q -t "oom-web:$FSHA" "$TMP"

step "서버 준비 대기 (첫 부팅의 Docker 설치)"
for i in $(seq 1 60); do $SSH 'test -f /var/lib/oom-user-data.done && docker version >/dev/null 2>&1' 2>/dev/null && break; sleep 10; done
$SSH 'docker version --format "docker {{.Server.Version}}" && free -m | sed -n 2,3p'

step "이미지 전송 (압축해서 ssh 로 — 수 분)"
docker save "oom-api:$SHA" "oom-ai:$SHA" "oom-web:$FSHA" | gzip | $SSH 'gunzip | docker load'

step "서버의 레포를 같은 커밋으로 ($SHA)"
$SSH "set -e; [ -d oom/.git ] || git clone -q '$REPO_URL' oom; cd oom && git fetch -q origin && git checkout -q --detach $SHA && git log --oneline -1"

step "운영 설정 올리기 (deploy/.env, 이미지 태그 반영)"
sed -e "s|^API_IMAGE=.*|API_IMAGE=oom-api:$SHA|" -e "s|^AI_IMAGE=.*|AI_IMAGE=oom-ai:$SHA|" -e "s|^WEB_IMAGE=.*|WEB_IMAGE=oom-web:$FSHA|" deploy/.env.prod \
  | $SSH 'umask 077; cat > oom/deploy/.env'

step "기동 — bootstrap.sh ${*:-(seed data eval)}"
$SSH "cd oom/deploy && bash bootstrap.sh $*"

step "확인"
ORIGIN=$(grep ^SITE_ORIGIN= deploy/.env.prod | cut -d= -f2)
for i in $(seq 1 30); do s=$(curl -ks -o /dev/null -w '%{http_code}' "$ORIGIN/api/v1/actuator/health" || true); [ "$s" = 200 ] && break; sleep 5; done
echo "health $s · 대문 $(curl -ks -o /dev/null -w '%{http_code}' "$ORIGIN/")"
$SSH 'docker stats --no-stream --format "  {{.Name}}\t{{.MemUsage}}" | grep oom-prod; free -m | sed -n 2,3p'
echo; echo "주소: $ORIGIN   (backend $SHA · web $FSHA, $(( $(date +%s) - t0 ))s)"
