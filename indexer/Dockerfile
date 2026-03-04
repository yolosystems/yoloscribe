FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY indexer/pyproject.toml indexer/uv.lock indexer/
WORKDIR /app/indexer
RUN uv sync --no-dev --frozen
COPY indexer/indexer/ indexer/
CMD ["uv", "run", "polling-worker"]
