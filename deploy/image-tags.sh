#!/usr/bin/env bash
# 이미지 태그 = 그 이미지에 들어가는 경로를 마지막으로 바꾼 커밋. 규정 · 데이터 · 배포 설정만 바뀐 커밋은 태그가 그대로라 빌드 · 교체를 건너뛴다.
# 2-ship.sh(PC 배포) · images 워크플로(GitHub) · autodeploy.sh(서버)가 같은 규칙을 쓴다.
# 사용: 레포 루트에서  eval "$(bash deploy/image-tags.sh [커밋, 기본 HEAD])"   → API_TAG · AI_TAG
set -euo pipefail
REF=${1:-HEAD}
# ai 이미지가 복사하는 경로를 그 커밋의 Dockerfile 에서 읽는다(다른 이미지에서 가져오는 COPY --from 은 뺀다)
AI_SRC=$(git show "$REF:ai/Dockerfile" | awk '/^COPY / && !/--from/ {for (i = 2; i < NF; i++) print $i}')
echo "API_TAG=$(git log -1 --format=%h "$REF" -- api)"
echo "AI_TAG=$(git log -1 --format=%h "$REF" -- ai/Dockerfile $AI_SRC)"
