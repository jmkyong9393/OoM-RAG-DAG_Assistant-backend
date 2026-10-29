# Python AI 서비스 이미지 (uvicorn). 같은 이미지로 정제 워커도 띄운다: command 를 `python -m app.worker` 로.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
# 의존성 레이어를 소스와 분리 — 코드만 바뀌면 캐시 재사용. graph extra = kiwi(형태소) + langgraph
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra graph --no-install-project
COPY app app
RUN uv sync --frozen --no-dev --extra graph
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
