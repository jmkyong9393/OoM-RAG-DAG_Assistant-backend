#!/usr/bin/env bash
# 자동 배포(당겨 오기): 서버가 새 버전을 스스로 확인해 무중단 교체한다. cron 이 2분마다 부른다(2-ship.sh 가 예약) — 바뀐 것이 없으면 바로 끝난다.
# 조건: 백엔드 main 의 CI 통과 + 그 커밋 · 프론트 main 의 이미지가 GHCR 에 있음(images 워크플로가 올린다). 서버로 들어오는 포트 · 비밀값이 필요 없다.
# 기록: ~/oom-deploy/auto.log · deployed(지금 조합) · failed(실패한 조합 — 새 커밋이 올 때까지 다시 하지 않음) · pause(있으면 쉼, 수동 배포 중)
# 사용: 서버 ~/oom 에서  bash deploy/autodeploy.sh   (손으로 돌려도 같다)
# 실행 중에 git checkout 으로 이 파일이 바뀌므로, 함수로 감싸 전체를 먼저 읽고 실행한다
main() {
  set -uo pipefail
  local REPO=${AUTODEPLOY_REPO:-jmkyong9393/OoM-RAG-DAG_Assistant-backend}
  local FRONT=${AUTODEPLOY_FRONT:-https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant.git}
  local REG=${AUTODEPLOY_REGISTRY:-ghcr.io/jmkyong9393}
  local S=$HOME/oom-deploy; mkdir -p "$S"
  log() { echo "$(date '+%F %T') $*" >> "$S/auto.log"; }

  exec 9>"$S/lock"; flock -n 9 || return 0          # 앞 배포가 아직 도는 중
  [ -f "$S/pause" ] && return 0                     # 수동 배포(2-ship.sh) 중

  git fetch -q origin main 2>/dev/null || { log "git fetch 실패 — 다음에 다시"; return 0; }
  local SHA FULL FSHA API_TAG AI_TAG
  FULL=$(git rev-parse origin/main); SHA=${FULL:0:7}
  FSHA=$(git ls-remote "$FRONT" refs/heads/main 2>/dev/null | cut -c1-7)
  [ -n "$FSHA" ] || { log "프론트 main 조회 실패 — 다음에 다시"; return 0; }
  eval "$(bash deploy/image-tags.sh origin/main)"
  local want="$SHA $API_TAG $AI_TAG $FSHA" have
  have=$(cat "$S/deployed" 2>/dev/null || true)
  [ "$want" = "$have" ] && return 0
  [ "$want" = "$(cat "$S/failed" 2>/dev/null || true)" ] && return 0

  # 백엔드 커밋이 바뀌었으면 그 커밋의 CI(main push) 결과를 확인한다 — 공개 API(무인증 시간당 60회), 새 커밋일 때만 부른다
  if [ "$SHA" != "${have%% *}" ]; then
    local ci
    ci=$(curl -fsS --max-time 20 "https://api.github.com/repos/$REPO/actions/workflows/ci.yml/runs?head_sha=$FULL&event=push&per_page=1" \
         | python3 -c 'import json,sys; r=(json.load(sys.stdin).get("workflow_runs") or [{}])[0]; print(r.get("status","none"), r.get("conclusion") or "-")' 2>/dev/null)
    case "$ci" in
      "completed success") ;;
      completed*) echo "$want" > "$S/failed"; log "CI 가 통과하지 못한 커밋($SHA: $ci) — 배포하지 않고 고친 커밋을 기다린다"; return 0 ;;
      *) return 0 ;;                                # 아직 도는 중 · 조회 실패 — 다음에 다시
    esac
  fi
  # 이미지가 GHCR 에 올라왔는지(빌드는 CI 뒤에 시작해 몇 분 걸린다). 공개 이미지라 로그인 없이 받는다
  local img
  for img in "oom-api:$API_TAG" "oom-ai:$AI_TAG" "oom-web:$FSHA"; do
    docker image inspect "$REG/$img" >/dev/null 2>&1 || docker pull -q "$REG/$img" >/dev/null 2>&1 || return 0
  done

  log "배포 시작: [$have] → [$want]"
  git checkout -q --detach "$FULL"
  cp deploy/.env deploy/.env.prev                   # 직전 설정 — rollback.sh 가 쓴다
  sed -i -e "s|^API_IMAGE=.*|API_IMAGE=$REG/oom-api:$API_TAG|" -e "s|^AI_IMAGE=.*|AI_IMAGE=$REG/oom-ai:$AI_TAG|" \
         -e "s|^WEB_IMAGE=.*|WEB_IMAGE=$REG/oom-web:$FSHA|" deploy/.env
  if (cd deploy && bash bootstrap.sh) >> "$S/auto.log" 2>&1; then
    echo "$want" > "$S/deployed"; rm -f "$S/failed"
    bash deploy/trim-images.sh "api=$API_TAG" "ai=$AI_TAG" "web=$FSHA" >> "$S/auto.log" 2>&1
    log "배포 끝: $want"
  else
    # 롤링 교체는 실패한 서비스의 옛 컨테이너를 그대로 둔다 — 서비스는 계속 돈다. 같은 조합을 2분마다 되풀이하지 않게 적어 둔다
    echo "$want" > "$S/failed"
    log "배포 실패: $want — 옛 컨테이너가 계속 돈다. 고친 커밋이 main 에 오면 다시 시도. 되돌리기: cd ~/oom/deploy && bash rollback.sh"
  fi
}
main "$@"; exit
