# OoM-RAG-DAG_Assistant-backend

**생성형 AI 기반 기업 업무지식 검색·활용 Assistant (OoM Assist)** 의 백엔드 레포.
프론트는 [OoM-RAG-DAG_Assistant](https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant) (Next.js 16, Vercel).

팀 OoM (Out of Memory): 배종현(팀장·AI·어시스턴트 화면), 장문경(백엔드 총괄·DB·배포·설계문서·AI 공동), 신동준(Spring 구현·데이터·평가), 홍경표(프론트)
이 레포의 머지 권한은 장문경에게 있다. `ai/`의 그래프·프롬프트는 배종현이 머지한다.
기간: 2026-10-12 ~ 2026-12-06 (8주, 6주차 금요일 코드 프리즈)

## 무엇을 하는가

공식 규정·지침 문서에 대한 **Hybrid RAG**(형태소 어휘 + 벡터, RRF)와, 실무자가 등록한 성공·실패 로그를 부모–자식으로 엮은 **계층형 트러블슈팅 DAG**를 LangGraph Supervisor가 결합해, 출처가 검증된 답변을 SSE로 스트리밍한다.

## 구조

```
[Next.js (Vercel)]
     │ REST + SSE
     ▼
[api/  Spring Boot 4.1 · Java 21]  CRUD · 워크스페이스 · 로그/간선 · 체인 · 대시보드 · SSE 릴레이 · DB 쓰기 전담
     │
     ├──▶ [ai/  Python · FastAPI]    classify → (policy_rag ∥ graph_search) → gate → generate → critic
     │                               kiwi 토크나이징 · 임베딩 · LLM 호출 · 정제 워커 (정제 컬럼만 쓰기)
     ▼
[PostgreSQL 16 + pgvector]         hybrid_search_*() · get_chain() · would_create_cycle() 함수 공유
```

| 폴더 | 내용 | 담당 |
|---|---|---|
| `api/` | Spring Boot 게이트웨이 (도메인 API · SSE 릴레이 · Flyway `db/migration`) | 신동준(1차 작성) → 장문경(결재·머지) |
| `ai/` | Python AI 서비스 (LangGraph, kiwipiepy, OpenAI, 정제 워커) | 배종현(그래프·프롬프트) · 장문경(DB 계층·워커) |
| `data/` | 규정 JSON 청크 · 합성 로그 · 평가셋 (git 커밋, 1급 자산) | 신동준 · 장문경(합성 로그) |
| `scripts/` | 원천(PDF·HWP·Markdown) → JSON, 인덱싱, 합성 로그 생성 | 신동준 |
| `eval/` | hit@5 · RAGAS 스크립트 | 신동준 |
| `deploy/` | docker-compose(로컬·프로덕션) · Caddyfile · `.env.example` | 장문경 |
| `docs/` | 개발문서 (인덱스 `docs/00_문서_인덱스.md`) | 장문경 |

경계 원칙 세 가지. **DB 쓰기는 `api/`만**, **검색·체인 SQL은 `db/` 함수로 공유**, **토크나이징·임베딩·LLM은 `ai/`만.**

## 시작하기

```bash
cp deploy/.env.example deploy/.env    # 로컬 기본값으로 바로 동작. OpenAI 키가 없으면 AI 는 fake 모드(규칙 분류·템플릿 답변)로 돈다
make up                               # postgres(pgvector) :5432
make migrate                          # Flyway 컨테이너로 스키마·함수·뷰 적용 (Spring 기동 시에도 자동 적용)
make seed-reset                       # data/seed/*.json 적재 (워크스페이스 4 · 사용자 4 · 로그 13 · 문서 6)
make api                              # Spring Boot :8080   (/api/v1, 환경변수는 deploy/.env)
make ai                               # Python AI :8000   (AI_MODE=auto: 키 있으면 openai, 없으면 fake)
make worker ARGS=--once               # 시드 직후 1회: 토큰 백필 + pending 로그 정제. 상시 실행은 ARGS 없이, 토크나이저/임베딩 모델 변경 후엔 ARGS=--reindex
```

`AI_MODE` — `openai`(OpenAI 호환 API, 정식 경로) · `fake`(키 없는 개발용: 실제 검색 + 규칙 기반 LLM 대체) · `dummy`(고정 이벤트, DB 불필요) · `auto`(기본: 키 있으면 openai, 없으면 fake). 공급자는 `LLM_PROFILE_<NAME>_*` env 프로필로 바꾼다. 키를 넣은 뒤 `scripts/llm_smoke.py` 로 4경로(분류·생성·정제·임베딩)를 1회 점검한다.

확인:

```bash
curl -c cj -X POST localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' -d '{"employee_no":"OOM0002","password":"Oom!demo-2026"}'
curl -N -b cj -X POST localhost:8080/api/v1/chat -H 'Content-Type: application/json' -H 'X-Workspace-Id: 1' -d '{"query":"E-403 어떻게 해결했어?"}'
```

테스트: `make test` (Spring, Docker 필요 — Testcontainers pgvector), `make test-ai` (Python, DB 통합 테스트는 로컬 Postgres 가 떠 있을 때만). 호스트에서 포트가 겹치면 `deploy/.env` 의 `POSTGRES_PORT`·`PORT`·`AI_PORT` 와 URL 들만 바꾼다.
계약·에러 코드·프론트 전환 체크리스트는 `docs/06_서비스_계약_Spring_Python_프론트.md`. SSE payload JSON Schema 는 `data/contracts/sse_events.schema.json` (`scripts/export_sse_schema.py` 로 생성).

## 문서

설계·결정·검토 문서는 `docs/`에 미러한다 (현재 팀 내부 폴더에서 관리 중, 공개 범위 합의 후 커밋). 처음 읽는 사람은 `docs/00_문서_인덱스.md` → `docs/01_프로젝트_총정리.md` 순서.
