FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY discord-bot/pyproject.toml discord-bot/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY discord-bot/discord_bot/ ./discord_bot/

RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "discord_bot"]
