#!/bin/bash
# Lightsail 인스턴스 첫 부팅 때 root 로 한 번 도는 스크립트(1-create-instance.sh 가 --user-data 로 넘긴다).
# 스왑 2GB + Docker 공식 저장소 설치(docs.docker.com/engine/install/ubuntu). 끝나면 /var/lib/oom-user-data.done 을 남긴다 — 2-ship.sh 가 이 파일을 기다린다.
set -eux
export DEBIAN_FRONTEND=noninteractive

# 4GB 박스에서 이미지 적재 · 첫 색인 때 여유를 둔다. 컨테이너 안(시험)처럼 스왑을 못 만드는 곳에서는 SKIP_SWAP=1
if [ "${SKIP_SWAP:-0}" != 1 ] && ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

id ubuntu >/dev/null 2>&1 && usermod -aG docker ubuntu   # ssh 로 들어온 ubuntu 가 sudo 없이 docker 를 쓴다
touch /var/lib/oom-user-data.done
