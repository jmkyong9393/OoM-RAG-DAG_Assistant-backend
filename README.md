# OoM-RAG-DAG_Assistant-backend

**생성형 AI 기반 기업 업무지식 검색·활용 Assistant (OoM Assist)** 의 백엔드 레포.
프론트는 [OoM-RAG-DAG_Assistant](https://github.com/Jonghyeon-Bae/OoM-RAG-DAG_Assistant) (Next.js 16, Vercel).

팀 OoM (Out of Memory): 배종현(팀장·AI), 장문경(백엔드·DB), 신동준(데이터·평가), 홍경표(프론트)
기간: 2026-10-12 ~ 2026-12-06 (8주, 6주차 금요일 코드 프리즈)

## 무엇을 하는가

공식 규정·지침 문서에 대한 **Hybrid RAG**(형태소 어휘 + 벡터, RRF)와, 실무자가 등록한 성공·실패 로그를 부모–자식으로 엮은 **계층형 트러블슈팅 DAG**를 LangGraph Supervisor가 결합해, 출처가 검증된 답변을 SSE로 스트리밍한다.

## 구조

```
[Next.js (Vercel)]
     │ REST + SSE
     ▼
[api/  Spring Boot 3 · Java 21]   CRUD · 워크스페이스 · 로그/간선 · 체인 · 대시보드 · SSE 릴레이 · DB 쓰기 전담
     │
     ├──▶ [ai/  Python · LangGraph]  classify → (policy_rag ∥ graph_search) → gate → generate → critic
     │                               kiwi 토크나이징 · 임베딩 · LLM 호출 · DB 읽기 전용
     ▼
[PostgreSQL 16 + pgvector]         hybrid_search_*() · chain_family() · would_create_cycle() 함수 공유
```

| 폴더 | 내용 | 담당 |
|---|---|---|
| `api/` | Spring Boot API 서버 | 장문경 |
| `ai/` | Python AI 서비스 (LangGraph, kiwipiepy, OpenAI) | 배종현 |
| `db/` | `schema.sql` · `functions.sql` · `roles.sql` · 시드 | 장문경 |
| `data/` | 규정 JSON 청크 · 합성 로그 · 평가셋 (git 커밋, 1급 자산) | 신동준 |
| `scripts/` | 원천(PDF·HWP·Markdown) → JSON, 합성 로그 생성 | 신동준 |
| `eval/` | hit@5 · RAGAS 스크립트 | 신동준 |
| `deploy/` | docker-compose(로컬·프로덕션) · Caddyfile · `.env.example` | 장문경 |
| `docs/` | 개발문서 (인덱스 `docs/00_문서_인덱스.md`) | 전원 |

경계 원칙 세 가지. **DB 쓰기는 `api/`만**, **검색·체인 SQL은 `db/` 함수로 공유**, **토크나이징·임베딩·LLM은 `ai/`만.**

## 시작하기 (계획)

```bash
cp deploy/.env.example deploy/.env      # OPENAI_API_KEY 등 채우기
make up                                 # postgres (pgvector) 기동 + schema/functions 적용
make seed                               # 워크스페이스 3종 · 규정 JSON · 합성 로그 적재
make api                                # Spring Boot :8080
make ai                                 # Python AI :8000
```

## 문서

설계·결정·검토 문서는 `docs/`에 미러한다 (현재 팀 내부 폴더에서 관리 중, 공개 범위 합의 후 커밋). 처음 읽는 사람은 `docs/00_문서_인덱스.md` → `docs/01_프로젝트_총정리.md` 순서.
