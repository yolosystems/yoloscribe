FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY libs/ libs/
COPY agent-runner/pyproject.toml agent-runner/uv.lock agent-runner/
WORKDIR /app/agent-runner
RUN uv sync --no-dev --frozen
COPY agent-runner/agent_runner/ agent_runner/
CMD ["uv", "run", "polling-worker"]
