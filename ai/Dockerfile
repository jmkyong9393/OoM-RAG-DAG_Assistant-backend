# Python AI 서비스 이미지 (uvicorn). 같은 이미지로 정제 워커도 띄운다: command 를 `python -m app.worker` 로.
# 빌드 맥락은 레포 루트(deploy/docker-compose.prod.yml) — 규정 업로드 변환이 scripts/ 의 변환 · 색인 모듈을 함께 쓴다.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/
# 규정 업로드 변환 도구: 스캔 PDF OCR(Tesseract + 한국어) · Word(pandoc). 데비안 공식 패키지
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor pandoc && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    SCRIPTS_DIR=/app/scripts UPLOADS_DIR=/data/uploads
WORKDIR /app
# 의존성 레이어를 소스와 분리 — 코드만 바뀌면 캐시 재사용. graph extra = kiwi(형태소) + langgraph
COPY ai/pyproject.toml ai/uv.lock ./
RUN uv sync --frozen --no-dev --extra graph --no-install-project
COPY scripts/build_policy_json.py scripts/index_policy.py scripts/hwpx_to_md.py scripts/pdf_locate.py scripts/
COPY ai/app app
RUN uv sync --frozen --no-dev --extra graph
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
