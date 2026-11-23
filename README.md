# OoM Assist — 백엔드

회사 직원이 업무 중에 묻는 질문에 **그 회사의 규정 조문**과 **그 부서가 과거에 해결한 사례**를 근거로 답하는 업무지원 Assistant 의 백엔드입니다. 답변의 모든 문장에는 근거 번호가 붙고, 서버가 그 근거가 실제로 검색된 것인지 확인합니다.

- 프론트엔드: [OoM-RAG-DAG_Assistant](https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant) (Next.js)
- 구성: Spring Boot(API) + Python FastAPI(AI) + PostgreSQL(pgvector), 단일 호스트 배포
- 상태(2026-11-20): REST API 63개 · DB 마이그레이션 V1 ~ V19 · 자동 테스트 Spring 241 · Python 328

## 핵심 기능

| 기능 | 하는 일 |
|---|---|
| **규정 검색** | 형태소 어휘 검색과 벡터 검색을 합쳐 조문을 찾고, 상위 20개를 가벼운 모델이 다시 골라 5개를 근거로 씁니다. 구어체 · 돌려 말한 질문에서도 맞는 조문을 찾기 위한 것입니다 |
| **사례 체인** | 실무자가 남긴 문제 → 원인 → 해결 기록을 부모 · 자식으로 엮어 둡니다. 질문과 같은 문제를 다룬 체인을 찾아 답변에 싣습니다 |
| **업무 기록 정리** | 실무자가 메모처럼 적은 기록에서 오류 코드 · 키워드 · 한 문장 요약을 뽑아 몇 초 안에 검색되게 합니다. 기록에 적힌 것만 쓰고(문제만 적은 기록에 해결을 지어내지 않습니다), 뽑은 오류 코드는 기록에 실제로 적혀 있는지 코드가 확인합니다 |
| **규정과 사례의 연결** | 사례마다 "규정을 따랐다 · 규정과 달리 처리했다 · 규정에 기준이 없다"를 적어 두고, 답변이 그 관계를 함께 말합니다 |
| **인용 검증** | 답변의 근거 표시(`[P1]` 규정 · `[N3]` 사례)가 실제 검색 결과에 있는지 확인해 스트리밍(SSE)으로 내보냅니다 |
| **규정 개정 대응** | 규정이 개정되면 그 규정을 따른 사례를 조문 단위로 대조합니다. 답변은 사례가 개정 전 기준임을 밝히고 지금 기준을 조문에서 찾아 말합니다 |
| **장애 보고서** | 장애 건 하나를 보고서로 만듭니다. 날짜 · 경과 · 원인 · 조치는 기록 그대로 옮기고, 요약 문장만 AI 가 근거 번호와 함께 씁니다. 내부용과 외부 공개용(이름 · 직책을 가림)을 나눕니다 |
| **재발 방지 표시** | 팀이 기록에 "재발 방지 · 후속 조치" 표시를 붙이면 보고서의 그 칸을 표시된 기록으로 채웁니다. 다시 만들 때 손대지 않은 기록의 문장은 바뀌지 않습니다 |
| **규정 파일 업로드** | PDF(글자 · 스캔) · Word · 한글 · MD 파일을 올리면 조문 단위로 자동 변환합니다. 관리자가 검수 · 승인한 뒤에만 검색에 쓰입니다 |
| **계정 · 권한** | 사번 로그인과 서버 세션, 회사 관리자 · 부서 관리자 권한, 계정 잠금 · 비활성화 · 삭제, 접속 기록 |

조직은 **회사 > 부서** 구조입니다. 규정은 회사에 속하고, 부서는 자기 문서와 전사 공통 문서를 함께 검색합니다. 부서마다 관리자가 규정 사용 · 사례 사용을 켜고 끕니다.

## 구조

```
[브라우저] ── Caddy ──┬── /           ▶ web  (Next.js, 프론트 레포의 이미지)
                      └── /api/v1/*   ▶ api  (Spring Boot 4.1 · Java 21)
                                          │   인증 · 권한 · 기록 · 체인 · 문서 · 보고서 · 대시보드 · SSE 중계
                                          ├──▶ ai  (Python · FastAPI)
                                          │        질문 분류 → 규정 검색 ∥ 사례 검색 → 근거 연결 → 답변 생성 → 인용 검증
                                          │        정제 워커: 기록 정제 · 규정 업로드 변환 · 보고서의 AI 문장
                                          ▼
                                   PostgreSQL 16 + pgvector  (검색 · 체인 조회는 DB 함수로 공유)
```

경계는 세 가지입니다.

- **DB 쓰기는 `api`** 가 합니다. `ai` 는 정제 결과 · 규정 적재 · 보고서의 AI 칸만 씁니다.
- **검색과 체인 조회는 DB 함수**로 두어 두 서비스가 같은 SQL 을 씁니다.
- **토크나이징 · 임베딩 · LLM 호출은 `ai`** 만 합니다.

### 설계에서 고른 것

| 고른 것 | 이유 |
|---|---|
| **DB 하나로** — PostgreSQL 에 pgvector 와 재귀 질의 | 벡터 검색용 · 그래프용 DB 를 따로 두지 않습니다. 규정 검색, 사례 체인, 트랜잭션, 로그인 세션을 한 DB 에서 처리해 데이터가 어긋날 자리를 줄였습니다 |
| **인용은 AI 가 아니라 코드가 확인** | 답변의 근거 표시를 검색 결과와 대조해 확인합니다. AI 를 한 번 더 불러 검증하는 방식보다 빠르고 비용이 들지 않으며, 결과가 매번 같습니다 |
| **사실과 AI 문장을 분리** | 장애 보고서의 날짜 · 경과 · 원인 · 조치는 서버가 기록에서 그대로 조립합니다. AI 는 요약 문장만 근거 번호와 함께 쓰고, 기록에 없는 내용을 쓰면 서버가 걸러 냅니다 |
| **고르는 일은 사람, 옮겨 쓰는 일은 AI** | 원인과 해결은 팀이 기록에 표시한 것만 따르고, 재발 방지도 표시가 있으면 그것으로 채웁니다. AI 가 스스로 고르게 두면 기록에 없는 원인을 지어내는 일이 있었습니다 |

### 폴더

| 폴더 | 내용 |
|---|---|
| `api/` | Spring Boot API 서버. DB 마이그레이션(`src/main/resources/db/migration`) |
| `ai/` | Python AI 서비스와 정제 워커 |
| `data/` | 규정 원문(`policies/`) · 합성 사례(`logs/`) · 규정↔사례 대조표(`alignment/`) · 체인 기록(`chain_records/`) · 평가셋(`evalset/` · `answer_checks/` · `eval_sample/`) · 시드(`seed/`) |
| `scripts/` | 규정 변환 · 적재, 사례 적재, 데이터 검사기, 시드 |
| `eval/` | 검색 · 답변 · 보고서 품질 측정 도구 |
| `deploy/` | 로컬 · 운영 compose, Caddy 설정, 첫 기동 스크립트, `.env.example` |

## 빠른 시작

Docker, Java 21, [uv](https://docs.astral.sh/uv/) 가 필요합니다. Windows 에서는 Git Bash 나 WSL 에서 실행합니다. `make` 가 없으면 `Makefile` 의 명령을 그대로 복사해 씁니다.

```bash
cp deploy/.env.example deploy/.env   # 로컬 기본값으로 바로 동작합니다
make up                              # PostgreSQL
make migrate                         # 스키마 · 함수 · 뷰
make seed                            # 회사 2 · 워크스페이스 8(전사 공통 2 · 부서 6) · 사용자 10
make index                           # 규정 → 조문 → DB
make api                             # API 서버  :8080   (다른 터미널)
make ai                              # AI 서비스 :8000   (다른 터미널)
make load-logs                       # 합성 사례 284건 (api 가 떠 있어야 합니다)
make worker ARGS=--once              # 사례 정제 · 임베딩
make link-policies                   # 사례 ↔ 근거 조문
make link-chains                     # 장애 건의 해결 기록
```

확인:

```bash
set -a; . deploy/.env; set +a
curl -c cj -X POST localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"employee_no\":\"OOM0002\",\"password\":\"$SEED_PASSWORD\"}"
curl -N -b cj -X POST localhost:8080/api/v1/chat -H 'Content-Type: application/json' -H 'X-Workspace-Id: 1' \
  --data-binary '{"query":"JVM 힙 비율은 얼마로 잡았어?"}'
```

| 알아 둘 것 | 내용 |
|---|---|
| 시드 계정 | 유통 `OOM0001` ~ `OOM0005`, IT 서비스 `OIT0001` ~ `OIT0005`. 0001번이 회사 관리자입니다. 비밀번호는 `deploy/.env` 의 `SEED_PASSWORD` |
| AI 키가 없으면 | 검색은 실제로 하고 답변은 규칙으로 만든 흉내 답변이 나옵니다(`AI_MODE=auto` 의 기본 동작). 키는 `deploy/.env` 의 `LLM_PROFILE_DEFAULT_API_KEY` 에 넣습니다 |
| 다시 돌려도 됩니다 | 적재 명령은 모두 여러 번 돌려도 결과가 같습니다. `make seed-reset` 만 업무 테이블을 비웁니다 |

## 설정

`deploy/.env` 한 곳에서 바꿉니다. 자주 쓰는 것만 적습니다.

| 변수 | 뜻 |
|---|---|
| `AI_MODE` | `auto`(기본 — 키가 있으면 실제 AI, 없으면 흉내) · `openai` · `fake` · `dummy` |
| `LLM_PROFILE_DEFAULT_*` | AI 공급자 주소 · 키 · 예비 키 · 모델(답변 · 가벼운 모델 · 임베딩) |
| `POLICY_RERANK` | `0` 이면 규정 재순위를 끕니다(질문마다 가벼운 모델 호출 1회가 빠집니다) |
| `CHAIN_SUMMARY_AUTO` | `false` 면 장애 건 AI 요약을 새로 만들지 않습니다 |
| `REPORT_AI` | `false` 면 장애 보고서를 AI 문장 없이 만듭니다 |
| `WORKER_EMBEDDED` | `1` 이면 정제 워커가 AI 서비스 안에서 같이 돕니다(운영 기본) |

임베딩 모델을 바꾸면 저장된 벡터를 모두 다시 만들어야 합니다 — `make worker ARGS=--reindex`.

## API

모든 경로는 `/api/v1` 아래에 있습니다. 로그인은 세션 쿠키, 부서는 `X-Workspace-Id` 헤더로 고릅니다.

| 영역 | 경로 |
|---|---|
| 로그인 · 내 정보 | `/auth/*`(로그인 · 로그아웃 · 동의 · 비밀번호 변경) · `/me` |
| 질문 | `POST /chat`(SSE) · `/sessions` · `/feedback` |
| 사례 기록 | `/logs`(등록 · 조회 · 상태 · 원인과 재발 방지 표시) · `/edges` · `/chains/{id}` |
| 규정과 사례의 연결 | `/logs/{id}/policy-refs` · `/logs/policy-articles` · `/policy-revisions/impact` |
| 장애 보고서 | `POST /reports` · `GET /reports` · `GET /reports/{id}` |
| 규정 문서 | `/documents`(목록 · 조문 · 폐기 · 복원 · 삭제) · `/documents/uploads`(올리기 · 검수 · 승인 · 반려) |
| 계정 · 부서 | `/users`(초대 · 역할 · 재발급 · 잠금 해제 · 비활성화 · 삭제) · `/workspaces` · `/audit` |
| 대시보드 | `GET /dashboard/summary` |

스트리밍 이벤트의 형식은 `data/contracts/sse_events.schema.json` 에 있습니다.

## 데이터 넣기

| 무엇 | 원본 | 넣는 법 |
|---|---|---|
| 규정 | `data/policies/src/<slug>/<slug>.md` + `meta.json` | `python scripts/validate_policy_pack.py --require-questions` → `make index` |
| 규정 개정판 | `data/policies/revisions/<slug>/` | `make index-revision SLUG=<slug>` — 옛 판을 폐기하고 영향받는 사례 수를 냅니다 |
| 화면에서 올린 규정 | 관리자가 올린 파일 | AI 워커가 변환 → 검수 → 승인 뒤 등록. 스캔 PDF 는 Tesseract, Word 는 pandoc 이 필요합니다(운영 이미지에 포함) |
| 사례 | `data/logs/<세트>_v1.json` | `python scripts/validate_logs.py` → `make load-logs` → `make worker ARGS=--once` |
| 규정↔사례 대조표 | `data/alignment/<회사>.json` | `python scripts/check_alignment.py` → `make link-policies` |
| 체인 기록(해결 기록 · 연결 사유) | `data/chain_records/<회사>.json` | `make link-chains` |
| 평가 결과(대시보드용) | `data/eval/eval_runs.json` | `make load-eval` |

규정을 고칠 때는 적재된 문서를 수정하지 않고 **새 slug + `supersedes`** 로 개정판을 냅니다. 적재된 문서를 고치면 조문 id 가 밀려 평가셋이 깨집니다. 집필 형식은 `data/policies/README.md`, 사례 형식은 `data/logs/README.md` 에 있습니다.

데이터는 가상의 두 회사(유통 · IT 서비스)입니다. 회사마다 전사 공통과 부서 3곳(SW 개발 · 경영지원 · 고객지원)이 있어 워크스페이스는 모두 8개입니다. 모두 적재하면 사용 중인 규정 31종(조문 조각 1,218개), 사례 284건이 들어가고, 평가 질문은 602건입니다. 인사 기본 규정 10종(회사별 취업규칙 · 휴가·휴직 · 근무제도 · 출장·여비 · 복리후생·경조)은 모두 전사 공통에 있어 어느 부서에서 물어도 근거로 붙습니다. 실존 기업 · 서비스 이름은 쓰지 않습니다.

## 품질

2026-11-20 에 기능을 모두 반영한 코드로 전체를 측정한 값입니다.

| 측정 | 값 |
|---|---|
| 검색 hit@5 (질문 452건) | **0.989** — 규정 313 / 317 · 사례 134 / 135 |
| 검색 MRR@5 | 0.90 |
| 답변 점검표 (48문항 × 2회) | 96 / 96 — 인용한 근거 · 들어 있어야 할 말 · 들어 있으면 안 되는 말 |
| 규정이 개정된 상황의 답변 (5문항 × 2회) | 10 / 10 |
| 답변 충실도 (48문항, 채점 모델 `gpt-4.1`) | 0.95 — 문장 161 / 169 가 근거로 뒷받침됨 |
| 답변 관련성 (같은 48문항) | 0.64 — 무관한 질문끼리는 0.27 |

위 값은 인사 기본 규정 10종을 넣기 전의 것입니다. 2026-11-23 에 10종을 넣은 뒤 검색만 다시 쟀습니다 — 같은 452건은 0.980(규정 309 / 317 · 사례 134 / 135)으로 4건 줄었고, 새 질문 150건은 147건을 맞혀 602건 전체는 0.980 입니다. 줄어든 문항은 정답이 검색 11 ~ 17위에 있다가 재순위로 살아나던 것들로, 전사 공통 조각이 늘면서 재순위 후보 20개 밖으로 밀렸습니다. 답변 점검표 전체와 답변 품질은 다시 재지 않았습니다(핵심 점검 묶음과 회사별 점검표만 확인).

## 테스트와 평가

| 무엇 | 명령 | 비고 |
|---|---|---|
| Spring 테스트 | `make test` | Docker 필요(Testcontainers) |
| Python 테스트 | `make test-ai` | DB 통합 테스트는 로컬 PostgreSQL 이 떠 있을 때만 |
| 검색 품질 | `cd ai && uv run python ../eval/hit_at_k.py --rerank` | 어휘 · 벡터 · 합친 것 · 재순위의 hit@k 와 MRR |
| 답변 점검 | `cd ai && uv run python ../eval/answer_checks.py --runs 2` | 실제 답변을 만듭니다(AI 호출) |
| 답변 품질 | `cd ai && uv run python ../eval/answer_quality.py --judge-model <모델>` | 충실도 · 관련성 채점 |
| 보고서 문장 점검 | `cd ai && uv run python ../eval/report_checks.py --list --json <경로>` | api 가 떠 있어야 합니다 |
| 전체 측정 | `make eval-full OUT=<결과 폴더>` | 검색 · 답변 점검 · 개정 상태 · 답변 품질을 차례로 돌리고 대시보드에 반영합니다. AI 호출이 많습니다(한 번에 약 2달러) |

- **변경 확인은 대표 묶음으로 합니다** — `data/eval_sample/`(평가 질문 43건 · 핵심 점검 12항목, 전체의 약 10%). 전체 측정은 AI 호출이 많아 매번 돌리지 않습니다. 답변에 넘어가는 근거를 바꾸는 데이터 변경 뒤에도 묶음을 돌립니다.
- **대시보드의 평가 값은 측정이 끝나는 순간 바뀝니다.** `make eval-full OUT=<결과 폴더>` 가 전체를 측정하면서 검색 · 답변 품질을 DB 에 기록하고(`--record`), 배포용 파일 `data/eval/eval_runs.json` 도 다시 만듭니다. 서버에서는 측정을 돌리지 않으므로 그 파일을 커밋하면 `make load-eval`(운영은 `bootstrap` 의 `eval` 단계)이 옮깁니다. 측정 시각이 같아 두 번 들어가지 않습니다.
- 실험으로 스크립트를 따로 돌릴 때는 `--record` 를 붙이지 않는 한 대시보드를 건드리지 않습니다(다른 임베딩 · 재순위 없는 검색 값이 대시보드를 덮어쓰지 않게).
- PR 마다 CI(`.github/workflows/ci.yml`)가 돕니다 — 키 패턴 · 금지 단어 검사, Python 테스트, Spring 테스트, 규정 원문 변환 점검.

## 배포

`deploy/docker-compose.prod.yml` 과 `deploy/Caddyfile` 로 한 대의 서버에 올립니다. Caddy 가 `/api/v1/*` 를 api 로, 나머지를 web 으로 보냅니다.

> 현재 단계(2026-11-20): 운영 구성을 로컬에서 끝까지 확인했습니다(빈 DB → 데이터 적재 → 로그인 · 질문). 실제 서버 배포는 아직입니다.

```bash
# 빈 서버에 처음 올릴 때
cd deploy && cp .env.example .env    # 운영 값 채우기 (SITE_ADDRESS · WEB_IMAGE …)
bash bootstrap.sh                    # DB → 마이그레이션 → 데이터 적재 → ai · web · caddy

# 운영 구성을 로컬에서 먼저 확인할 때
cd deploy && cp .env.example .env.smoke && bash smoke.sh
```

- 서버의 레포 체크아웃과 api · ai 이미지는 **같은 커밋**이어야 합니다. 어긋나면 api 가 뜰 때 뷰 재적용이 실패합니다.
- 모든 단계는 다시 돌려도 됩니다. 데이터만 고쳤으면 `bash bootstrap.sh data`.
- 규정 업로드의 원본 · 검수본은 볼륨 `oom-prod-uploads` 에 있습니다. 서버를 옮길 때 DB 와 함께 백업합니다.
- 첫 적재는 약 4분 걸리고 AI 호출이 있습니다(조문 임베딩과 사례 정제).
- `SITE_ADDRESS` 에 도메인을 넣으면 HTTPS 가 자동으로 붙습니다.

## 협업

- `main` 에 직접 커밋하지 않습니다. 브랜치 → PR → CI 통과 → 머지.
- 공개 레포입니다. API 키 · 비밀번호 · 실존 기업 이름을 커밋하지 않습니다(CI 가 검사합니다).
- 설계 · 계약 · 작업 기록은 팀 내부 문서로 관리합니다.

팀 OoM (Out of Memory) — 배종현(팀장 · 프론트) · 장문경(백엔드 · DB · 배포) · 신동준(백엔드 · 데이터 · 평가) · 홍경표(프론트)
