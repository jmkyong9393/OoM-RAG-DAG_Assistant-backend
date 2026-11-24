# Lightsail 단일 서버 배포

처음 한 번은 개발 PC 에서 명령 네 줄로 서버를 만들고 띄운다(이미지를 직접 보낸다). 그 뒤로는 **자동 배포** — `main` 에 머지하면 CI → 이미지 빌드(GHCR) → 서버가 2분 안에 당겨 와 무중단 교체한다. 서버로 들어오는 포트 · GitHub 비밀값이 필요 없다.

| 자동 배포 | |
|---|---|
| 흐름 | 백엔드 `main` 의 `ci` 통과 → `images` 워크플로가 api · ai 를 빌드해 `ghcr.io/<owner>/oom-*` 로 올림(프론트 `main` 은 15분마다 확인해 web 을 빌드) → 서버 cron(2분)의 `deploy/autodeploy.sh` 가 새 조합을 보고 CI 통과 · 이미지 존재를 확인한 뒤 `bootstrap.sh`(배포 직전 백업 → 롤링 교체 → 데이터 단계) |
| 기록 | 서버 `~/oom-deploy/auto.log` · `deployed`(지금 조합: 커밋 · api · ai · web 태그) · `failed`(실패한 조합 — 새 커밋이 올 때까지 다시 하지 않음) |
| 멈추기 | 서버에서 `touch ~/oom-deploy/pause`(다시 켜기 `rm`). `2-ship.sh` 는 도는 동안 스스로 멈춘다 |
| 되돌리기 | 서버 `cd ~/oom/deploy && bash rollback.sh` — 직전 이미지로 롤링 교체. 고친 커밋을 `main` 에 올리면 그 커밋으로 다시 자동 배포된다 |

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
| `2-ship.sh <IP> [bootstrap 인자]` | 첫 배포 · 수동 배포(자동 배포를 우회할 때). 서버는 GitHub 에서 같은 커밋을 받으므로 먼저 커밋 · 푸시한다. 프론트 레포는 `FRONT_DIR`(기본 `../OoM-RAG-DAG_Assistant`)의 `FRONT_REF`(기본 `origin/main`)를 읽기만 한다. 끝에 자동 배포 예약(`/etc/cron.d/oom-autodeploy`)을 설치한다 |
| `3-destroy.sh` | 인스턴스와 고정 IP 삭제(과금 중지). 서버의 DB · 업로드도 사라진다 |

배포 전 리허설(실제 프론트 이미지까지 운영 compose 로): `cd deploy && WEB_IMAGE=<프론트 이미지> bash smoke.sh --web --quick`
