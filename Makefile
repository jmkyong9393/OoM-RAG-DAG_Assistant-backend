# 로컬 개발 진입점. Windows는 Git Bash 또는 WSL에서 실행.
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file deploy/.env

.PHONY: up down migrate api ai worker seed seed-reset test test-ai psql logs

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

worker:        ## 정제 워커 (pending 로그 정제 · 토큰/임베딩 백필). make worker ARGS=--once 는 1회만
	set -a; . deploy/.env; set +a; cd ai && uv run python -m app.worker $(ARGS)

test-ai:       ## Python 테스트 (DB 통합 테스트는 로컬 Postgres 가 떠 있을 때만 실행)
	set -a; . deploy/.env; set +a; cd ai && uv sync -q --extra graph && uv run pytest -q

psql:          ## DB 셸
	docker exec -it oom-postgres psql -U oom -d oom

logs:
	$(COMPOSE) logs -f postgres
