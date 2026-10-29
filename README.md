# OoM-RAG-DAG_Assistant-backend

**생성형 AI 기반 기업 업무지식 검색·활용 Assistant (OoM Assist)** 의 백엔드 레포.
프론트는 [OoM-RAG-DAG_Assistant](https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant) (Next.js 16). 운영은 **Lightsail 1대 단일 호스트**(Caddy → web / api / ai / postgres)로 배포한다.

팀 OoM (Out of Memory): 배종현(팀장·AI·프론트), 장문경(백엔드 총괄·DB·배포·설계문서), 신동준(백엔드·데이터·평가), 홍경표(프론트)
이 레포의 머지 권한은 장문경에게 있다. `main` 직접 커밋 금지, 브랜치 → PR → CI 통과 → 머지.
기간: 2026-10-12 ~ 2026-12-06 (8주, 6주차 금요일 코드 프리즈). 코어 게이트 11/06.

## 무엇을 하는가

공식 규정·지침 문서에 대한 **Hybrid RAG**(형태소 어휘 + 벡터, RRF)와, 실무자가 등록한 성공·실패 로그를 부모–자식으로 엮은 **계층형 트러블슈팅 DAG**를 결합해, 출처가 검증된 답변을 SSE로 스트리밍한다. 사번 로그인 + 서버 세션 쿠키, 워크스페이스(부서) 단위 격리와 상속(전사 공통 → 부서).

## 구조

```
[브라우저] ── Caddy ──┬── /            ▶ [web  Next.js 16 컨테이너]   (프론트 레포 이미지)
                      └── /api/v1/*    ▶ [api  Spring Boot 4.1 · Java 21]  인증·세션 · 워크스페이스 · 로그/간선 · 체인 · 문서 · 대시보드 · SSE 릴레이 · DB 쓰기 전담
                                              │
                                              ├──▶ [ai  Python · FastAPI]   classify(규칙 → LLM) → (policy_rag ∥ graph_search) → gate → generate → critic
                                              │                             kiwi 토크나이징 · 임베딩 · LLM 호출 · 정제 워커(같은 프로세스, WORKER_EMBEDDED)
                                              ▼
                                       [PostgreSQL 16 + pgvector]   hybrid_search_chunks/nodes() · get_chain() · would_create_cycle() 함수 공유, 역할 oom_api / oom_ai
```

| 폴더 | 내용 | 담당 |
|---|---|---|
| `api/` | Spring Boot 게이트웨이 (도메인 API · 사번 로그인·세션 · SSE 릴레이 · Flyway `db/migration` V1~V7 + R__functions/views) · `Dockerfile` | 장문경 · 신동준 |
| `ai/` | Python AI 서비스 (파이프라인, kiwipiepy, OpenAI 호환 LLM, 정제 워커) · `Dockerfile` | 장문경 · 배종현(그래프·프롬프트) |
| `data/` | 규정 청크 JSON(`policies/`, 원문 `policies/src/<slug>/`) · 합성 로그(`logs/`) · 평가셋(`evalset/`) · 시드 · SSE 계약 스키마(`contracts/`) | 신동준(적재·평가셋) · 프론트팀(규정 집필) |
| `scripts/` | 원문(HWPX·Markdown) → 청크 JSON(`hwpx_to_md.py`, `build_policy_json.py`), 인덱싱(`index_policy.py`), 시드, LLM 스모크 | 신동준 · 장문경 |
| `eval/` | `hit_at_k.py`(어휘/벡터/하이브리드 hit@k·MRR) | 신동준 |
| `deploy/` | `docker-compose.yml`(로컬: postgres만) · `docker-compose.prod.yml`(단일 호스트 운영) · `Caddyfile` · `docker-compose.smoke.yml` + `smoke.sh`(운영 compose 로컬 검증) · `.env.example` | 장문경 |
| `.github/workflows/` | `ci.yml`(가드·Python·Spring·규정 변환 점검) · `deploy.yml`(2단계 자리표시) | 장문경 |

경계 원칙 세 가지. **DB 쓰기는 `api/`만**(Python 은 정제 컬럼·규정 적재만, `oom_ai` 역할), **검색·체인 SQL은 `db/` 함수로 공유**, **토크나이징·임베딩·LLM은 `ai/`만.**

## 현재 상태 (2026-10-29)

- Spring API: 사번+비밀번호 로그인(5회 실패 15분 잠금, 180일 만료, 첫 로그인 동의·비밀번호 변경), 워크스페이스, 로그/간선/체인(다중 부모 bulk `parent_refs`), 문서 상태, 피드백, 대시보드, SSE 릴레이. 테스트 99.
- Python AI: 규칙 우선 의도 분류(LLM 은 애매할 때만, 평가셋 71% 규칙 결정), 어휘(IDF 가중 + 제목 가중 분리 + 사용자 사전 + 동의어 확장) + 벡터 RRF, 답변 5문장 지시 + `ANSWER_MAX_TOKENS` 상한, 인용 검증. 테스트 72.
- 검색 품질(평가셋 45건): 하이브리드 **hit@5 42/45**, hit@1 22, MRR 0.77. 첫 토큰 웜 0.9~1.4 s.
- 데이터: 공정위 고시 1종 232청크(HWPX 원문), 시드 미니 문서 3종, 시드 로그 13건. 자체 규정 4종(프론트팀 집필)·합성 로그 100건·평가셋 150건은 진행 중(개발문서 `작업 지시/`).
- 배포: 운영 compose 로컬 스모크 통과(Caddy 경유 로그인·SSE 첫 토큰 2.3 s, web 제외 ≈0.9 GB). Lightsail 기동은 아직.

## 시작하기 (로컬 개발)

```bash
cp deploy/.env.example deploy/.env    # 로컬 기본값으로 바로 동작. OpenAI 키가 없으면 AI 는 fake 모드(규칙 분류·템플릿 답변)로 돈다
make up                               # postgres(pgvector)
make migrate                          # Flyway 컨테이너로 스키마·함수·뷰 적용 (Spring 기동 시에도 자동 적용)
make seed-reset                       # data/seed/*.json 적재 (워크스페이스 4 · 사용자 5 · 로그 13 · 문서 6)
make api                              # Spring Boot :8080 (/api/v1). "Started ..." 뒤 Gradle 이 80% EXECUTING 으로 머무는 게 정상. 종료는 Ctrl+C
make ai                               # Python AI :8000   (AI_MODE=auto: 키 있으면 openai, 없으면 fake)
make worker ARGS=--once               # 시드 직후 1회: 토큰 백필 + pending 로그 정제. 상시는 ARGS 없이. 토크나이저/사전/임베딩 모델 변경 후엔 ARGS=--reindex
make index                            # data/policies/*.chunks.json 규정 적재(개정 자동 폐기) + 백필. 검색 품질은 eval/hit_at_k.py
```

`make` 가 없으면 `Makefile` 의 명령을 그대로 복사해 쓴다. 시드 로그인: 사번 `OOM0001~0005` / 비밀번호 `Oom!demo-2026`(`SEED_PASSWORD`).

`AI_MODE` — `openai`(OpenAI 호환 API, 정식 경로) · `fake`(키 없는 개발용: 실제 검색 + 규칙 기반 LLM 대체) · `dummy`(고정 이벤트, DB 불필요) · `auto`(기본). 공급자는 `LLM_PROFILE_<NAME>_*` 프로필로 바꾼다. 키를 넣은 뒤 `scripts/llm_smoke.py` 로 분류·생성·정제·임베딩을 1회 점검한다.

확인:

```bash
curl -c cj -X POST localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' -d '{"employee_no":"OOM0002","password":"Oom!demo-2026"}'
curl -N -b cj -X POST localhost:8080/api/v1/chat -H 'Content-Type: application/json' -H 'X-Workspace-Id: 1' -d '{"query":"E-403 어떻게 해결했어?"}'
```

Git Bash 에서 한글 본문은 `--data-binary @file.json` 으로 보낸다(`-d '한글'` 은 인코딩이 깨져 400).

## 규정 문서 넣기

원문 `data/policies/src/<slug>/<slug>.md`(+`meta.json`, 집필 형식은 개발문서 프론트팀 지시서 §A) → `uv run scripts/build_policy_json.py ... --out data/policies` → `make index`. HWPX 원문은 `scripts/hwpx_to_md.py` 로 먼저 Markdown 화. 변환 규칙·slug 표는 `data/policies/README.md`. **개정은 새 slug + `supersedes`** (적재된 문서를 고치면 청크 id 가 밀려 평가셋이 깨진다).

## 테스트·CI

- `make test` (Spring 99, Docker 필요 — Testcontainers pgvector), `make test-ai` (Python 72, DB 통합 18건은 로컬 Postgres 가 떠 있을 때만).
- PR 마다 `ci.yml`: `guard`(API 키 패턴·공개 레포 금지 단어) · `python` · `spring` · `policy-data`(규정 원문 PR 에서 변환기를 돌려 청크 수·형식 위반을 Job Summary 로).
- 검색 품질: `cd ai && uv run python ../eval/hit_at_k.py` (평가셋 `data/evalset/*.json`, 3모드 hit@k·MRR).

## 운영 배포 (단일 호스트)

`deploy/docker-compose.prod.yml` + `deploy/Caddyfile`. Caddy 가 `/api/v1/*` → api(SSE `flush_interval -1`), 나머지 → web. api 는 `oom_api` 역할로 접속하고 내장 Flyway 가 관리자 계정으로 마이그레이션한다. 정제 워커는 ai 프로세스 안에서 돈다(`WORKER_EMBEDDED=1`). 필요한 변수는 `.env.example` 의 운영 절(`SITE_ADDRESS`·`SITE_ORIGIN`·`WEB_IMAGE` …).

```bash
cd deploy && cp .env.example .env.smoke   # 값 채우기 (WEB_IMAGE 는 프론트 레포 Dockerfile 로 만든 이미지)
bash smoke.sh                             # 로컬에서 운영 compose 전체를 띄워 마이그레이션 → 시드 → Caddy 경유 로그인·SSE 까지 확인 후 내림 (--keep 이면 유지)
```

Lightsail 에서는 `docker compose -f docker-compose.prod.yml --env-file .env pull && up -d`. `SITE_ADDRESS` 에 도메인을 넣으면 HTTPS 자동.

## 문서

설계·결정·검토·작업일지는 팀 내부 `개발문서/` 폴더에서 관리한다(인덱스 `00_문서_인덱스.md` → 요약 `01` → 설계 `02` → 계약 `06` → 산출물 `07`). 공개 범위 합의 후 `docs/` 로 미러한다. 계약·에러 코드·프론트 전환 체크리스트는 `06_서비스_계약_Spring_Python_프론트.md`, SSE payload JSON Schema 는 `data/contracts/sse_events.schema.json`(`scripts/export_sse_schema.py` 로 생성).
