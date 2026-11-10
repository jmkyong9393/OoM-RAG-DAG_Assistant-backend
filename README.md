# OoM-RAG-DAG_Assistant-backend

**생성형 AI 기반 기업 업무지식 검색·활용 Assistant (OoM Assist)** 의 백엔드 레포.
프론트는 [OoM-RAG-DAG_Assistant](https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant) (Next.js 16). 운영은 **Lightsail 1대 단일 호스트**(Caddy → web / api / ai / postgres)로 배포한다.

팀 OoM (Out of Memory): 배종현(팀장·AI·프론트), 장문경(백엔드 총괄·DB·배포·설계문서), 신동준(백엔드·데이터·평가), 홍경표(프론트)
이 레포의 머지 권한은 장문경에게 있다. `main` 직접 커밋 금지, 브랜치 → PR → CI 통과 → 머지.
기간: 2026-10-12 ~ 2026-12-06 (8주, 6주차 금요일 코드 프리즈). 코어 게이트 11/06.

## 무엇을 하는가

회사의 부서 직원이 업무 중에 묻는 질문에, **그 회사의 규정 조문**과 **그 부서가 과거에 처리한 사례**를 근거로 답한다.

- **규정 검색(Hybrid RAG)** — 형태소 어휘 검색과 벡터 검색을 합쳐 조문을 찾는다.
- **사례 체인(트러블슈팅 DAG)** — 실무자가 등록한 문제 → 원인 → 해결 기록을 부모–자식으로 엮는다. 질문과 같은 문제를 다룬 체인을 찾아 답에 싣는다.
- **규정과 사례의 연결** — 사례마다 "어느 조문을 따랐는지 / 규정과 달리 처리했는지 / 규정에 기준이 없는지"를 적어 두고, 답변이 그 관계를 함께 말한다. 예: "당시에는 힙을 75% 로 잡았으나 현재 기준은 70% 다."
- **인용 검증** — 답변의 근거 마커(`[P1]`, `[N3]`)가 실제로 검색된 근거인지 확인해 SSE 로 스트리밍한다.
- **규정 개정 대응** — 규정이 개정되면 그 규정을 따른 사례를 조문 단위로 대조해(그대로 · 조문 개정 · 대응 조문 없음), 답변이 "개정 전 기준"임을 밝히고 영향받는 사례 목록을 낸다(`GET /policy-revisions/impact`).

조직은 **회사(테넌트) > 부서(워크스페이스)** 다. 규정은 회사에 속하고, 부서는 자기 문서와 전사 공통 문서를 함께 검색한다. 부서마다 관리자가 **규정 사용·사례 사용**을 켜고 끈다(규정 참조형 · 규정 + 사례형 · 사례 단일형). 사번 로그인 + 서버 세션 쿠키.

## 구조

```
[브라우저] ── Caddy ──┬── /            ▶ [web  Next.js 16 컨테이너]   (프론트 레포 이미지)
                      └── /api/v1/*    ▶ [api  Spring Boot 4.1 · Java 21]  인증·세션 · 부서 · 로그/간선 · 체인 · 문서 · 대시보드 · SSE 릴레이 · DB 쓰기 전담
                                              │
                                              ├──▶ [ai  Python · FastAPI]   분류(규칙 → LLM) → 규정 검색 ∥ 사례 검색·관련성 판정 → 근거 연결 → 생성 → 인용 검증
                                              │                             kiwi 토크나이징 · 임베딩 · LLM 호출 · 정제 워커(같은 프로세스, WORKER_EMBEDDED)
                                              ▼
                                       [PostgreSQL 16 + pgvector]   hybrid_search_chunks() · search_case_entry() · get_chain() 등 함수 공유, 역할 oom_api / oom_ai
```

| 폴더 | 내용 | 담당 |
|---|---|---|
| `api/` | Spring Boot 게이트웨이 (도메인 API · 사번 로그인·세션 · SSE 릴레이 · Flyway `db/migration` V1~V9 + R__functions/views) · `Dockerfile` | 장문경 · 신동준 |
| `ai/` | Python AI 서비스 (파이프라인, kiwipiepy, OpenAI 호환 LLM, 정제 워커) · `Dockerfile` | 장문경 · 배종현(그래프·프롬프트) |
| `data/` | 규정(`policies/`, 원문 `policies/src/<slug>/`, 개정판 `policies/revisions/`) · 합성 사례 로그(`logs/`) · 규정↔사례 대조표(`alignment/`) · 사례 체인 기록(`chain_records/` — 해결 기록·연결 사유) · 평가셋(`evalset/`) · 답변 점검표(`answer_checks/`) · 시드 · SSE 계약 스키마(`contracts/`) | 신동준(적재·평가셋) · 프론트팀(규정 집필) |
| `scripts/` | 원문 → 청크 JSON(`hwpx_to_md.py`, `build_policy_json.py`, `pdf_locate.py`), 적재(`index_policy.py`, `load_logs.py`, `load_alignment.py`), 검사기(`validate_policy_pack.py`, `validate_logs.py`, `check_alignment.py`), 평가셋 생성, 시드, LLM 스모크 | 신동준 · 장문경 |
| `eval/` | `hit_at_k.py`(검색 hit@k·MRR, 대시보드 기록) · `answer_checks.py`(실제 답변 점검) · `answer_quality.py`(충실도·관련성 채점, 대시보드 기록) | 신동준 · 장문경 |
| `deploy/` | `docker-compose.yml`(로컬: postgres만) · `docker-compose.prod.yml`(단일 호스트 운영) · `Caddyfile` · `bootstrap.sh` + `bootstrap-data.sh`(빈 DB 데이터 적재) · `docker-compose.smoke.yml` + `smoke.sh`(운영 compose 로컬 검증) · `.env.example` | 장문경 |
| `.github/workflows/` | `ci.yml`(가드·Python·Spring·규정 변환 점검) · `deploy.yml`(2단계 자리표시) | 장문경 |

경계 원칙 세 가지. **DB 쓰기는 `api/`만**(Python 은 정제 컬럼·규정 적재만, `oom_ai` 역할), **검색·체인 SQL은 DB 함수로 공유**, **토크나이징·임베딩·LLM은 `ai/`만.**

## 현재 상태 (2026-11-07)

**데이터** — 가상의 두 회사. 실존 기업·서비스 이름은 쓰지 않는다.

| 회사 | 부서 (id) | 규정 | 사례 |
|---|---|---|---|
| OoM 리테일(유통) | 전사 공통 10 · SW 개발 1 · 경영지원 2 · 고객지원 3 | 13종 + 외부 고시 1종 + 시드 FAQ 1종 | 131건 + 시드 13건 |
| OoM 테크(IT 서비스) | 전사 공통 20 · SW 개발 21 · 경영지원 22 · 고객지원 23 | 6종 | 140건 |

활성 규정 21종 850청크(시연용 개정판 3종 — 배포·장애 대응 운영지침, 상품 교환·반품·환불 규정, 채용 규정 — 은 `data/policies/revisions/`), 사례 284건, 사례와 규정을 잇는 대조표 261줄(따름 · 예외 · 기준 없음).

**검색** — 임베딩 `text-embedding-3-large`(1536 차원으로 줄여 받음). 어휘(단어별 IDF · 제목 가중 분리 · 사용자 사전 · 동의어) + 벡터를 RRF 로 합치되, 벡터 상위 2건과 어휘 1위는 앞에 둔다. 규정은 그 상위 20 을 가벼운 모델이 다시 골라(재순위, 검색 상위 2건은 남김) 상위 5를 근거로 쓴다 — 구어·우회 표현 질문의 정답 조문이 6~20위에 머물던 문제. 사례는 거리 하한을 넘은 후보만 가벼운 모델이 "같은 문제인가"를 판정해 붙인다.

| 지표 (평가셋 452건) | 값 |
|---|---|
| 규정 질의 hit@5 | 313 / 317 (재순위 전 302) |
| 사례 질의 hit@5 | 134 / 135 |
| MRR@5 · DB 검색 시간 p50 | 0.90 (재순위 전 0.82) · 49 ms |
| 사례 질의에서 정답 체인을 진입점으로 고름 | 126 / 135 |
| 주제만 비슷한 질문에 사례가 붙음 | 2 / 24 |
| 답변 충실도 (점검 질문 48건, 채점 모델 `gpt-4.1`) | 0.97 — 문장 172 / 179 |
| 답변 관련성 (같은 48건, `text-embedding-3-large`) | 0.66 — 무관한 질문끼리는 0.26 |
| 인용 검증 통과 · 첫 글자까지 (같은 48건) | 인용 모두 검증 · p50 2.0초 (재순위를 끄면 1.8초) |

**답변** — 점검표 4종(유통 13 · IT 11 · 부서별 추천 질문 18 · 어려운 질문 6)을 실제 답변으로 돌려 통과. 점검은 인용한 조문·사례, 들어 있어야 할 말, 들어 있으면 안 되는 말을 본다.

**API** — 사번+비밀번호 로그인(5회 실패 15분 잠금, 180일 만료, 첫 로그인 동의·비밀번호 변경), 부서 목록·설정(`GET /workspaces` · `/workspaces/manageable` · `PATCH /workspaces/{id}`), 로그/간선/체인, 사례의 근거 조문(`/logs/{id}/policy-refs`, `/logs/policy-articles`), 규정 개정 영향(`/policy-revisions/impact`), 규정 문서 관리(`/documents`), 규정 파일 업로드 · 자동 변환 · 검수(`/documents/uploads`), 피드백, 대시보드(`retrieval` = 검색 품질 실측), SSE 릴레이. 계약은 개발문서 `06`.

**테스트** — Spring 150, Python 193(DB 통합 포함).

**배포** — 운영 compose 로컬 스모크 통과(빈 DB → `bootstrap.sh` 데이터 적재 → Caddy 경유 로그인·SSE). Lightsail 기동은 아직.

## 시작하기 (로컬 개발)

```bash
cp deploy/.env.example deploy/.env    # 로컬 기본값으로 바로 동작. OpenAI 키가 없으면 AI 는 fake 모드(규칙 분류·템플릿 답변, 어휘 검색만)로 돈다
make up                               # postgres(pgvector)
make migrate                          # Flyway 컨테이너로 스키마·함수·뷰 적용 (Spring 기동 시에도 자동 적용)
make seed-reset                       # data/seed/*.json 적재 (회사 2 · 부서 8 · 사용자 10 · 로그 13 · 부서별 추천 질문)
make index                            # data/policies/src 의 규정 → 청크 → DB 적재(개정 자동 폐기, 부서 이동) + 토큰·임베딩 백필
make api                              # Spring Boot :8080 (/api/v1). "Started ..." 뒤 Gradle 이 80% EXECUTING 으로 머무는 게 정상. 종료는 Ctrl+C
make ai                               # Python AI :8000   (AI_MODE=auto: 키 있으면 openai, 없으면 fake)
make load-logs                        # data/logs/*_v1.json 합성 사례 적재 (api 가 떠 있어야 한다)
make worker ARGS=--once               # 토큰 백필 + pending 로그 정제(키워드·요약·임베딩). 상시는 ARGS 없이
make link-policies                    # data/alignment/*.json → 사례의 근거 조문 연결
make link-chains                      # data/chain_records/*.json → 장애 건을 해결한 기록 · 간선 연결 사유
```

`make` 가 없으면 `Makefile` 의 명령을 그대로 복사해 쓴다. 시드 로그인: 유통 `OOM0001~0005`, IT `OIT0001~0005` / 비밀번호 `Oom!demo-2026`(`SEED_PASSWORD`). `OOM0001`·`OIT0001` 이 회사 관리자다.

`AI_MODE` — `openai`(OpenAI 호환 API, 정식 경로) · `fake`(키 없는 개발용: 실제 검색 + 규칙 기반 LLM 대체) · `dummy`(고정 이벤트, DB 불필요) · `auto`(기본). 공급자와 모델은 `LLM_PROFILE_<NAME>_*` 프로필로 바꾼다(`_MODEL_LIGHT` 분류·판정·정제, `_MODEL_ANSWER` 답변, `_EMBEDDING_MODEL`). `POLICY_RERANK=0` 이면 규정 재순위를 끈다(질문마다 가벼운 모델 1회 호출이 빠진다). 키를 넣은 뒤 `scripts/llm_smoke.py` 로 분류·생성·정제·임베딩을 1회 점검한다.

임베딩 모델은 `LLM_PROFILE_<NAME>_EMBEDDING_MODEL`(기본 `text-embedding-3-large`, 1536 차원으로 줄여 받는다). **모델을 바꾸면 저장된 벡터를 전부 다시 만들어야 한다** — `make worker ARGS=--reindex`. 다시 만들기 전까지는 질의와 저장 벡터의 모델이 달라 벡터 검색을 끄고 어휘 검색만으로 답한다(AI 서비스 로그에 한 줄 경고). 사례 관련성 기준(`CASE_FLOOR`)은 모델별로 보정한 값이라, 목록에 없는 모델은 사례 관련성 판정 없이 동작한다.

확인:

```bash
curl -c cj -X POST localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' -d '{"employee_no":"OOM0002","password":"Oom!demo-2026"}'
curl -N -b cj -X POST localhost:8080/api/v1/chat -H 'Content-Type: application/json' -H 'X-Workspace-Id: 1' -d '{"query":"JVM 힙 비율은 얼마로 잡았어?"}'
```

Git Bash 에서 한글 본문은 `--data-binary @file.json` 으로 보낸다(`-d '한글'` 은 인코딩이 깨져 400).

## 데이터 넣기

**규정** — 원문 `data/policies/src/<slug>/<slug>.md` + `meta.json`(+ 평가 질의 `<slug>.questions.md`) → `make index`. 집필 형식과 변환 규칙은 `data/policies/README.md`. 올리기 전에 검사기를 돌린다.

```bash
python scripts/validate_policy_pack.py --require-questions    # 형식 · 조문 참조 · 숫자 · 같은 부서의 제목 충돌
```

- **개정은 새 slug + `supersedes`** (적재된 문서를 고치면 청크 id 가 밀려 평가셋이 깨진다). 시행 전 개정판은 `data/policies/revisions/` 에 두고 `make index-revision SLUG=…` 로만 적재한다. 적재하면 영향받는 사례 수가 출력되고(조문 그대로 · 조문 개정 · 대응 조문 확인 필요), 사례의 근거 조문은 개정 사슬을 따라 지금 조문으로 이어진다.
- 모든 직원에게 적용되는 규정(급여, 원격접속)은 전사 공통 부서에 둔다. `meta.json` 의 `workspace_id` 를 바꾸고 `make index` 하면 문서가 옮겨진다.
- HWPX 원문은 `scripts/hwpx_to_md.py` 로 먼저 Markdown 화.
- **화면에서 올리기** — 관리자가 PDF(글자 · 스캔) · Word · HWP · HWPX · MD 를 `POST /documents/uploads` 로 올리면 AI 워커가 같은 변환기(`build_policy_json` · `index_policy`)로 조문 MD 를 만들고, 검수 · 승인 뒤 등록한다(스캔본은 Tesseract OCR, 원본과 검수본은 `UPLOADS_DIR` 에 보관). 로컬은 `data/uploads/`(gitignore). OCR · Word 변환은 tesseract · pandoc 이 있어야 해서 로컬 `make ai` 에서는 PDF 글자층 · HWP · MD 만 된다 — 운영 이미지에는 들어 있다.

**사례 로그** — `data/logs/<세트>_v1.json`(형식은 `data/logs/README.md`) → `python scripts/validate_logs.py` → `make load-logs` → `make worker ARGS=--once`.

**규정↔사례 대조표** — `data/alignment/<회사>.json` 에 사례마다 관계(따름 `follows` · 예외 `exception` · 기준 없음 `gap`)를 적는다 → `python scripts/check_alignment.py` → `make link-policies`. 화면에서는 `PUT /logs/{id}/policy-refs` 로 넣는다.

**사례 체인 기록** — 간선마다 상위에 이어 붙인 이유(`log_edges.reason`), 장애 건마다 실제로 해결한 기록(`log_resolutions`, 여러 개)을 남긴다. 화면·API 는 `POST /logs` 의 `edge_reason`, `POST /edges` 의 `reason`, `PATCH /logs/{id}` 의 `resolved_node_ids`. 유효한 해결 기록(활성 · 그 장애 건의 자손)은 DB 함수 `resolved_nodes()` 가 정하고, 답변은 그 줄을 먼저 인용하며 사후분석 같은 후속 조치와 구분한다. 시연용 기록은 `data/chain_records/<회사>.json` → `make link-chains` — 합성 사례 전체에 자동으로 채우지 않는다(어느 조치가 해결했는지 지어내지 않기 위해). 장애 건의 **원인**은 실패 기록의 `category='cause'`(상위가 있을 때만 `is_cause`)로 표시한다 — `POST /logs` 의 `category` · 한 번에 등록의 `cause_title`, `PATCH /logs/{id}` 의 `category`. 합성 사례는 제목이 '원인:' 인 55건에 표시했다. 원인을 묻는 질문에서 원인이 여럿이면 답변이 원인마다 그 해결을 짝지어 쓴다.

**부서별 추천 질문** — `data/seed/workspaces.json` 의 `settings.suggestions`. 넣은 질문은 `data/answer_checks/suggestions_v1.json` 에도 있어야 한다(테스트가 확인) — 근거가 붙는 답이 나오는 질문만 첫 화면에 낸다.

## 테스트·평가·CI

- `make test` (Spring, Docker 필요 — Testcontainers pgvector), `make test-ai` (Python, DB 통합 테스트는 로컬 Postgres 가 떠 있을 때만).
- PR 마다 `ci.yml`: `guard`(API 키 패턴·공개 레포 금지 단어) · `python` · `spring` · `policy-data`(규정 원문 PR 에서 변환기를 돌려 청크 수·형식 위반을 Job Summary 로).
- 검색 품질: `cd ai && uv run python ../eval/hit_at_k.py` (평가셋 `data/evalset/*.json`, 어휘/벡터/하이브리드 hit@k·MRR, 두 문서에 걸친 질의의 동시 적중). `--record` 를 붙이면 결과를 `eval_runs` 에 남겨 대시보드(`GET /dashboard/summary` 의 `retrieval`)에 실측값이 나온다.
- 답변 점검: `cd ai && uv run python ../eval/answer_checks.py --runs 2` (점검표 `data/answer_checks/*.json`, 실제 답변 생성 — LLM 호출 있음). 지시문이나 모델을 바꾼 뒤에는 이것을 돌린다.
- 답변 품질: `cd ai && uv run python ../eval/answer_quality.py --judge-model <채점 모델>` — 같은 점검표의 질문으로 답변을 만들고, 채점 모델이 충실도(답변 문장 중 컨텍스트로 뒷받침되는 비율)와 관련성(답변에서 되짚어 만든 질문과 원래 질문의 임베딩 유사도)을 낸다. 정의는 RAGAS 를 따른다. `--record` 를 붙이면 `eval_runs` 에 남아 대시보드(`eval`)에 나온다. 관련성은 임베딩 모델마다 척도가 달라, 무관한 질문끼리의 유사도(기준선)를 함께 출력한다.
- 저장된 결과 → 대시보드: `eval/export_eval_runs.py` 가 두 도구의 `--json` 결과를 `--record` 와 같은 행(+ 측정 시각)으로 바꿔 `data/eval/eval_runs.json` 에 쓰고, `make load-eval`(운영은 `bootstrap.sh` 의 `eval` 단계)이 넣는다. LLM 호출 없이 멱등 — 서버에서 평가를 다시 돌리지 않고 로컬 실측을 옮겨, 운영 대시보드가 "측정 전"으로 뜨지 않는다.

## 운영 배포 (단일 호스트)

`deploy/docker-compose.prod.yml` + `deploy/Caddyfile`. Caddy 가 `/api/v1/*` → api(SSE `flush_interval -1`), 나머지 → web. api 는 `oom_api` 역할로 접속하고 내장 Flyway 가 관리자 계정으로 마이그레이션한다. 정제 워커는 ai 프로세스 안에서 돈다(`WORKER_EMBEDDED=1`). 개인정보 처리방침 문안(`data/legal/`)은 api 컨테이너에 마운트한다. 필요한 변수는 `.env.example` 의 운영 절(`SITE_ADDRESS`·`SITE_ORIGIN`·`WEB_IMAGE` …).

로컬 검증:

```bash
cd deploy && cp .env.example .env.smoke   # 값 채우기 (WEB_IMAGE 는 프론트 레포 Dockerfile 로 만든 이미지)
bash smoke.sh                             # 운영 compose 전체: 마이그레이션 → 데이터 적재(bootstrap.sh) → Caddy 경유 로그인·SSE 확인 후 내림 (--keep 유지 · --quick 시드 + 평가 결과만)
```

박스 첫 기동 (빈 DB):

```bash
cd deploy && cp .env.example .env         # 운영 값. 레지스트리 이미지를 쓰면 먼저 docker compose -f docker-compose.prod.yml --env-file .env pull
bash bootstrap.sh                         # postgres → api(Flyway) → bootstrap 컨테이너(시드 · 규정 색인 · 사례 · 근거 연결 · 체인 기록 · 평가 결과) → ai · web · caddy
```

- 데이터 적재는 compose 네트워크 안의 일회성 `bootstrap` 컨테이너(AI 이미지 + `scripts/`·`data/` 읽기 전용 마운트)가 한다. postgres 포트를 호스트에 열지 않고, 사례는 API(`POST /logs/bulk`)로 넣어 도메인 규칙 검사를 같이 받는다.
- 박스의 레포 체크아웃과 api · ai 이미지는 같은 커밋이어야 한다. Flyway·`bootstrap` 은 레포의 마이그레이션·데이터를 쓰고 api 는 이미지 안의 것을 쓰므로, 레포가 더 새로우면 api 기동 때 뷰 재적용이 실패한다(`cannot drop columns from view`). 레포를 올렸으면 이미지도 같은 커밋으로 pull 한다.
- 모든 단계가 멱등이다. 데이터만 고쳤으면 `bash bootstrap.sh data` — 규정이 그대로면 재색인 없이 수 초, 바뀌었으면 재색인(그동안 1~2분 어휘 검색이 빈다).
- AI 이미지는 레포 루트를 빌드 맥락으로 쓴다(`ai/Dockerfile` — `scripts/` 의 변환 · 색인 모듈 4개를 넣고, 허용 목록은 `ai/Dockerfile.dockerignore`). 규정 업로드 변환용 tesseract-ocr(+kor) · pandoc 이 들어 있어 이미지가 약 1.2GB.
- 규정 업로드 원본 · 검수본은 볼륨 `oom-prod-uploads`(api · ai 가 `/data/uploads` 로 공유). 박스를 옮길 때 DB 와 함께 백업한다.
- 시연용 개정판은 `bash bootstrap.sh revision:<slug>` (`make index-revision` 과 같다 — 구판 폐기, 영향받는 사례 수 출력).
- 첫 적재는 약 4분(규정 860청크 임베딩 + 사례 284건 정제, OpenAI 호출 있음). 끝에 적재 수(활성 규정 21 · 청크 860 · 사례 284 · 근거 연결 256 · 해결 기록 9 · 연결 사유 19)를 출력한다.

이후 이미지 갱신은 `docker compose -f docker-compose.prod.yml --env-file .env pull && up -d`. `SITE_ADDRESS` 에 도메인을 넣으면 HTTPS 자동. 임베딩 모델을 바꿔 배포했으면 이어서 재색인한다(`docker compose … exec ai uv run --no-sync python -m app.worker --reindex`).

## 문서

설계·결정·검토·작업일지는 팀 내부 `개발문서/` 폴더에서 관리한다(인덱스 `00_문서_인덱스.md` → 요약 `01` → 설계 `02` → 계약 `06` → 산출물 `07` → 임베딩·검색 `22` → 규정·사례 체계 `36`). 공개 범위 합의 후 `docs/` 로 미러한다. 계약·에러 코드·프론트 전환 체크리스트는 `06_서비스_계약_Spring_Python_프론트.md`, SSE payload JSON Schema 는 `data/contracts/sse_events.schema.json`(`scripts/export_sse_schema.py` 로 생성).
