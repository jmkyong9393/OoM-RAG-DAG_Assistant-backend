# 로컬 개발 진입점. Windows는 Git Bash 또는 WSL에서 실행.
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file deploy/.env

.PHONY: pdf up down migrate api ai index worker seed seed-reset load-logs link-policies test test-ai psql logs

up:            ## postgres 기동
	$(COMPOSE) up -d postgres

down:          ## 컨테이너 정지 (데이터 유지)
	$(COMPOSE) down

migrate:       ## Spring 없이 Flyway 컨테이너로 마이그레이션 적용
	$(COMPOSE) --profile migrate run --rm flyway

api:           ## Spring Boot :$$PORT (기본 8080. deploy/.env 를 환경변수로, 기동 시 Flyway 적용)
	set -a; . deploy/.env; set +a; cd api && ./gradlew bootRun

ai:            ## Python AI 서비스 :$$AI_PORT (기본 8000. AI_MODE=dummy|fake|openai|auto)
	set -a; . deploy/.env; set +a; cd ai && uv sync -q --extra graph && uv run uvicorn app.main:app --host 127.0.0.1 --port "$${AI_PORT:-8000}" --reload

seed:          ## data/seed/*.json → DB (멱등, --reset 은 비우고 다시). DATABASE_URL 은 deploy/.env
	set -a; . deploy/.env; set +a; uv run --with "psycopg[binary]" --with bcrypt scripts/seed.py

seed-reset:
	set -a; . deploy/.env; set +a; uv run --with "psycopg[binary]" --with bcrypt scripts/seed.py --reset

test:          ## Spring 테스트 (Testcontainers → Docker 필요)
	cd api && ./gradlew test

index:         ## data/policies/src/*/meta.json → 변환(chunks.json, PDF 면 쪽 번호) → DB 적재(R8 개정 폐기·terms→사용자 사전) → 재토크나이즈·임베딩 백필. 재실행은 해시로 스킵
	set -a; . deploy/.env; set +a; \
	for m in data/policies/src/*/meta.json; do \
	  uv run scripts/build_policy_json.py --meta "$$m" || exit 1; \
	  d=$$(dirname "$$m"); s=$$(basename "$$d"); \
	  if [ -f "$$d/$$s.pdf" ]; then (cd ai && uv run --extra pdf python ../scripts/pdf_locate.py "../data/policies/$$s.chunks.json" --write) || exit 1; fi; \
	done; \
	uv run --with "psycopg[binary]" scripts/index_policy.py $(ARGS) && cd ai && uv run python -m app.worker --once --reindex

pdf:           ## PDF 쪽 번호 채우기 + 서빙 위치 복사. make pdf SLUG=vpn-guide-v30  (원문 data/policies/src/<slug>/<slug>.pdf → data/files/policies/)
	test -n "$(SLUG)" || { echo "SLUG=<slug> 필요"; exit 1; }
	cd ai && uv run --extra pdf python ../scripts/pdf_locate.py "../data/policies/$(SLUG).chunks.json" --write
	mkdir -p data/files/policies && cp "data/policies/src/$(SLUG)/$(SLUG).pdf" "data/files/policies/$(SLUG).pdf" && echo "copied → data/files/policies/$(SLUG).pdf"

load-logs:     ## data/logs/*_v1.json → POST /logs/bulk (멱등, source_ref) + 폐기 PATCH. api 가 떠 있어야 한다. 이어서 make worker ARGS=--once · make link-policies
	set -a; . deploy/.env; set +a; python scripts/load_logs.py

link-policies: ## data/alignment/*.json → 사례 노드의 근거 조문(attributes.policy_refs). make index · make load-logs 뒤에. 멱등
	set -a; . deploy/.env; set +a; uv run --with "psycopg[binary]" scripts/load_alignment.py $(ARGS)

worker:        ## 정제 워커 (pending 로그 정제 · 토큰/임베딩 백필). make worker ARGS=--once 는 1회만
	set -a; . deploy/.env; set +a; cd ai && uv run python -m app.worker $(ARGS)

test-ai:       ## Python 테스트 (DB 통합 테스트는 로컬 Postgres 가 떠 있을 때만 실행)
	set -a; . deploy/.env; set +a; cd ai && uv sync -q --extra graph --extra pdf && uv run pytest -q

psql:          ## DB 셸
	docker exec -it oom-postgres psql -U oom -d oom

logs:
	$(COMPOSE) logs -f postgres
