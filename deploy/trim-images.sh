#!/usr/bin/env bash
# 이 프로젝트 이미지(oom-api · oom-ai · oom-web)를 종류마다 "지금 쓰는 태그 + 직전 하나"만 남기고 지운다.
# 직전 하나는 되돌리기용 — 그 커밋으로 2-ship.sh 를 다시 돌리면 빌드 · 전송 없이 바로 뜬다.
# 태그 없이 남은 옛 층은 이 프로젝트 표식(label oom.project=assist)이 있는 것만 지운다 — 다른 프로젝트 이미지 · 빌드 캐시는 건드리지 않는다.
# 사용: bash trim-images.sh api=<태그> ai=<태그> web=<태그>   (개발 PC 와 서버에서 같은 스크립트. 'local' 태그는 스모크용이라 남긴다)
set -uo pipefail
for kv in "$@"; do
  name=${kv%%=*}; cur=${kv#*=}
  # 만든 시각이 최근인 순으로 직접 정렬한다(docker images 의 나열 순서는 그 순이 아닐 때가 있다) — 지금 태그와 local 을 빼고 첫 줄(직전)만 남긴다
  old=$(docker images "oom-$name" --format '{{.CreatedAt}}|{{.Tag}}' | sort -r | cut -d'|' -f2 | grep -vx -e "$cur" -e local -e '<none>' | tail -n +2)
  for t in $old; do docker rmi "oom-$name:$t" >/dev/null 2>&1 && echo "  지움 oom-$name:$t" || echo "  남김 oom-$name:$t (쓰는 컨테이너가 있음)"; done
done
docker image prune -f --filter label=oom.project=assist >/dev/null
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep -E '^oom-(api|ai|web):' | sed 's/^/  /'
