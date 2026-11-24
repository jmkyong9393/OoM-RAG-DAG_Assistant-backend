#!/usr/bin/env bash
# 이 프로젝트 이미지(oom-api · oom-ai · oom-web)를 종류마다 "지금 쓰는 태그 + 직전 하나"만 남기고 지운다.
# 직전 하나는 되돌리기용(rollback.sh) — 빌드 · 전송 없이 바로 뜬다.
# 로컬 이름(oom-api, 2-ship.sh)과 레지스트리 이름(ghcr.io/…/oom-api, 자동 배포)을 합쳐 태그 기준으로 센다 — 같은 태그는 한 벌로 본다.
# 태그 없이 남은 옛 층은 이 프로젝트 표식(label oom.project=assist)이 있는 것만 지운다 — 다른 프로젝트 이미지 · 빌드 캐시는 건드리지 않는다.
# 사용: bash trim-images.sh api=<태그> ai=<태그> web=<태그>   (개발 PC 와 서버에서 같은 스크립트. 'local' 태그는 스모크용이라 남긴다)
set -uo pipefail
for kv in "$@"; do
  name=${kv%%=*}; cur=${kv#*=}
  list=$(docker images --format '{{.CreatedAt}}|{{.Repository}}|{{.Tag}}' | awk -F'|' -v n="oom-$name" '$2 == n || $2 ~ ("/" n "$")')
  # 만든 시각이 최근인 순으로 태그를 세어, 지금 태그와 local 을 뺀 첫 태그(직전)만 남긴다
  keep=$(echo "$list" | sort -r | cut -d'|' -f3 | grep -vx -e "$cur" -e local -e '<none>' -e '' | awk '!s[$0]++' | head -1)
  echo "$list" | while IFS='|' read -r _ repo tag; do
    [ -z "$tag" ] && continue
    case "$tag" in "$cur"|local|"<none>"|"$keep") continue;; esac
    docker rmi "$repo:$tag" >/dev/null 2>&1 && echo "  지움 $repo:$tag" || echo "  남김 $repo:$tag (쓰는 컨테이너가 있음)"
  done
done
docker image prune -f --filter label=oom.project=assist >/dev/null
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep -E '(^|/)oom-(api|ai|web):' | sed 's/^/  /'
