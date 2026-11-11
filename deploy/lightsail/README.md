# Lightsail 단일 서버 배포

개발 PC 에서 명령 네 줄로 서버를 만들고 띄운다. 레지스트리 없이 이미지를 직접 보낸다.

```bash
aws login
bash deploy/lightsail/1-create-instance.sh       # 서버 + 고정 IP + 방화벽 (요금제 · 월 가격을 보여 주고 확인을 받는다)
bash deploy/lightsail/make-env.sh <IP>           # 운영 설정 deploy/.env.prod (비밀번호 새로 생성, gitignore)
bash deploy/lightsail/2-ship.sh <IP>             # 이미지 빌드 → 전송 → 데이터 적재 → 기동 → 확인
```

| 파일 | 하는 일 |
|---|---|
| `1-create-instance.sh` | 4GB Ubuntu 인스턴스, 키 쌍(`~/.ssh/oom-assist.pem`), 고정 IP, 방화벽(22 는 내 IP 만 · 80 · 443 공개). 요금제 · OS 는 계정에서 조회해 고른다 |
| `user-data.sh` | 첫 부팅 때 스왑 2GB + Docker 공식 저장소 설치 |
| `make-env.sh <IP> [도메인] [--http]` | 운영 설정 생성. 도메인이 없으면 `<IP>.sslip.io` 로 HTTPS. 이미 있으면 덮어쓰지 않는다(DB 비밀번호는 첫 기동 때 볼륨에 굳는다) |
| `2-ship.sh <IP> [bootstrap 인자]` | 다시 돌리면 갱신 배포. 서버는 GitHub 에서 같은 커밋을 받으므로 먼저 커밋 · 푸시한다. 프론트 레포는 `FRONT_DIR`(기본 `../OoM-RAG-DAG_Assistant`)의 `FRONT_REF`(기본 `origin/fronend`)를 읽기만 한다 |
| `3-destroy.sh` | 인스턴스와 고정 IP 삭제(과금 중지). 서버의 DB · 업로드도 사라진다 |

배포 전 리허설(실제 프론트 이미지까지 운영 compose 로): `cd deploy && WEB_IMAGE=<프론트 이미지> bash smoke.sh --web --quick`
