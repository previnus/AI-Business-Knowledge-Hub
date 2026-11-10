FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 TIKTOKEN_CACHE_DIR=/opt/tiktoken
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY hub ./hub
RUN .venv/bin/python -c "from hub.extract import encoding; encoding()" \
    && groupadd --gid 10001 hub \
    && useradd --uid 10001 --gid 10001 --no-create-home hub
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4).close()"]
CMD ["uvicorn", "hub.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "50"]
