#!/usr/bin/env bash
# 인스턴스와 고정 IP 를 지운다(과금 중지). 서버의 DB · 업로드 파일도 함께 사라진다 — 되돌릴 수 없다.
# 사용: bash deploy/lightsail/3-destroy.sh        (확인 문구를 입력해야 지운다)
# 환경: AWS_REGION(기본 ap-northeast-2) · INSTANCE_NAME(기본 oom-assist)
set -euo pipefail
REGION=${AWS_REGION:-ap-northeast-2}; NAME=${INSTANCE_NAME:-oom-assist}
L="aws lightsail --region $REGION"
$L get-instance --instance-name "$NAME" --query 'instance.[name,publicIpAddress,createdAt]' --output text
read -r -p "'$NAME' 인스턴스와 고정 IP 를 지웁니다. 인스턴스 이름을 그대로 입력: " a
[ "$a" = "$NAME" ] || { echo "취소"; exit 1; }
$L delete-instance --instance-name "$NAME" --query 'operations[0].status' --output text
$L release-static-ip --static-ip-name "$NAME-ip" --query 'operations[0].status' --output text || true   # 붙지 않은 고정 IP 는 과금된다
echo "지웠습니다. 키 쌍($NAME-key)과 로컬 키 파일(~/.ssh/$NAME.pem)은 남아 있습니다."
