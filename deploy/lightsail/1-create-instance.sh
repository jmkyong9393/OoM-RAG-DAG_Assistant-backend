#!/usr/bin/env bash
# Lightsail 인스턴스 1대를 만든다: 4GB Ubuntu + 첫 부팅 스크립트(Docker) + 고정 IP + 방화벽(22 는 내 IP 만, 80 · 443 공개).
# 사용(개발 PC, 레포 루트 아무 데서나):  bash deploy/lightsail/1-create-instance.sh [--yes]
# 준비: AWS CLI 로그인(aws login 또는 aws configure). 요금이 발생한다 — 만들기 전에 요금제와 월 가격을 보여 주고 확인을 받는다.
# 환경: AWS_REGION(기본 ap-northeast-2) · INSTANCE_NAME(기본 oom-assist) · RAM_GB(기본 4)
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # Windows 콘솔(cp949)에서 한글 출력 오류 방지
cd "$(dirname "$0")"
REGION=${AWS_REGION:-ap-northeast-2}; NAME=${INSTANCE_NAME:-oom-assist}; RAM=${RAM_GB:-4}
KEY_NAME="$NAME-key"; KEY_FILE="$HOME/.ssh/$NAME.pem"; IP_NAME="$NAME-ip"
PY=""; for c in python3 python py; do if "$c" -c "import json" >/dev/null 2>&1; then PY=$c; break; fi; done   # Windows 의 python3 은 실행되지 않는 스토어 껍데기일 수 있다
[ -n "$PY" ] || { echo "파이썬이 필요합니다"; exit 1; }
L="aws lightsail --region $REGION"

aws sts get-caller-identity --query Account --output text >/dev/null || { echo "AWS 로그인이 필요합니다: aws login"; exit 1; }
if $L get-instance --instance-name "$NAME" >/dev/null 2>&1; then
  echo "이미 '$NAME' 인스턴스가 있습니다. IP: $($L get-instance --instance-name "$NAME" --query instance.publicIpAddress --output text)"; exit 0
fi

# 요금제 · OS 는 이름을 박아 두지 않고 계정에서 조회해 고른다: RAM 이 같은 리눅스 요금제 중 공인 IPv4 가 있는 가장 싼 것, 가장 새 Ubuntu LTS
read -r BUNDLE PRICE CPU DISK <<<"$($L get-bundles --output json | "$PY" -c "
import json,sys
b=[x for x in json.load(sys.stdin)['bundles'] if x.get('isActive') and float(x['ramSizeInGb'])==float($RAM)
   and 'LINUX_UNIX' in x.get('supportedPlatforms',[]) and x.get('publicIpv4AddressCount',1)>=1]
b.sort(key=lambda x:x['price']); x=b[0]; print(x['bundleId'],x['price'],x['cpuCount'],x['diskSizeInGb'])")"
BLUEPRINT=$($L get-blueprints --output json | "$PY" -c "
import json,sys
b=[x['blueprintId'] for x in json.load(sys.stdin)['blueprints'] if x.get('isActive') and x.get('type')=='os' and x['blueprintId'].startswith('ubuntu_')]
print(sorted(b,key=lambda s:[int(n) for n in s.split('_')[1:] if n.isdigit()])[-1])")
AZ=${AZ:-${REGION}a}
MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')

cat <<EOF
만들 것 (리전 $REGION / 가용 영역 $AZ)
  인스턴스   $NAME — $BLUEPRINT, 요금제 $BUNDLE (RAM ${RAM}GB · vCPU $CPU · 디스크 ${DISK}GB)
  요금       월 \$$PRICE (시간 단위 과금 — 지우면 멈춘다)
  고정 IP    $IP_NAME (인스턴스에 붙어 있는 동안 무료)
  방화벽     22/tcp ← $MYIP/32 만, 80 · 443/tcp ← 전체
  SSH 키     $KEY_FILE
EOF
if [ "${1:-}" != "--yes" ]; then read -r -p "만들까요? (yes 입력) " a; [ "$a" = "yes" ] || { echo "취소"; exit 1; }; fi

if [ ! -f "$KEY_FILE" ]; then
  mkdir -p "$HOME/.ssh"
  $L create-key-pair --key-pair-name "$KEY_NAME" --query privateKeyBase64 --output text > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi
$L create-instances --instance-names "$NAME" --availability-zone "$AZ" --blueprint-id "$BLUEPRINT" --bundle-id "$BUNDLE" \
  --key-pair-name "$KEY_NAME" --user-data file://user-data.sh --query 'operations[0].status' --output text
echo -n "기동 대기"
until [ "$($L get-instance-state --instance-name "$NAME" --query state.name --output text 2>/dev/null)" = "running" ]; do echo -n .; sleep 5; done; echo
$L allocate-static-ip --static-ip-name "$IP_NAME" >/dev/null
$L attach-static-ip --static-ip-name "$IP_NAME" --instance-name "$NAME" >/dev/null
$L put-instance-public-ports --instance-name "$NAME" --port-infos \
  "fromPort=22,toPort=22,protocol=tcp,cidrs=$MYIP/32" "fromPort=80,toPort=80,protocol=tcp" "fromPort=443,toPort=443,protocol=tcp" >/dev/null
IP=$($L get-static-ip --static-ip-name "$IP_NAME" --query staticIp.ipAddress --output text)

cat <<EOF

완료. 고정 IP: $IP   (Docker 설치가 2~3분 더 걸린다 — 다음 단계가 기다린다)
다음:
  bash deploy/lightsail/make-env.sh $IP        # 운영 설정 파일 deploy/.env.prod 만들기
  bash deploy/lightsail/2-ship.sh $IP          # 이미지 빌드 → 전송 → 기동
EOF
