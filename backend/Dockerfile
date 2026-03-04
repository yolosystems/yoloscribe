# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Grab the uv binary from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Place the shared mcp_oauth library at /libs so that the relative path
# ../libs/mcp_oauth in backend/pyproject.toml resolves correctly when
# pyproject.toml sits at /app/pyproject.toml.
COPY libs/ /libs/

WORKDIR /app

# Install dependencies in a separate layer so they are cached between builds.
# --frozen requires uv.lock to exist — run `uv lock` locally before building.
# --no-install-project skips installing the app itself (not a package).
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy the pre-built virtual environment from the builder stage.
# mcp_oauth is installed into site-packages (non-editable), so /libs is not
# needed at runtime.
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY backend/main.py ./
COPY backend/agents/ ./agents/

# Run as a non-root user
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

EXPOSE 8000

# Single worker — scale horizontally via the K8s HPA rather than forking here
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
