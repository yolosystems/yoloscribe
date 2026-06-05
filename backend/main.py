"""YoloScribe backend — FastAPI service running behind a public ALB on EKS."""

import contextlib
import logging
import os

from fastapi import FastAPI
from log_setup import configure_logging

configure_logging()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, AWS_REGION, BEDROCK_EMBEDDING_MODEL, mcp_api_base, auth_provider, user_site_repo, s3, sqs_indexing, s3vectors
from config import LOCAL_MODE, LOCAL_SITE_NAME, LOCAL_USER_ID, LOCAL_MCP_API_KEY
from config import MAX_REQUEST_BYTES
from rate_limit import limiter
from mcp_server import create_mcp_app
from routers import (
    archive_router,
    assets_router,
    chat_router,
    content_router,
    health_router,
    mcp_oauth_router,
    oauth_router,
    obsidian_router,
    outbound_webhooks_router,
    pages_router,
    search_router,
    versions_router,
    settings_router,
    site_router,
    token_budget_router,
    tokens_router,
    tools_router,
    webhooks_router,
)

# ── MCP server ─────────────────────────────────────────────────────────────────

_mcp_app = None


@contextlib.asynccontextmanager
async def _lifespan(app):
    if _mcp_app is not None:
        async with _mcp_app.router.lifespan_context(app):
            yield
    else:
        yield


# ── OpenAPI tag metadata ───────────────────────────────────────────────────────

_OPENAPI_TAGS = [
    {"name": "health", "description": "Service liveness check."},
    {"name": "assets", "description": "Upload and serve media assets (images, video, audio) stored in S3."},
    {"name": "content", "description": "Read and write page content stored in S3."},
    {"name": "pages", "description": "List and create wiki pages within a site."},
    {"name": "agents", "description": "List AI agent definitions for a page."},
    {"name": "tools", "description": "List and manage top-level MCP tool definitions."},
    {"name": "skills", "description": "List and manage per-site skill definitions."},
    {"name": "settings", "description": "Read and update per-page access-control settings."},
    {"name": "access", "description": "Request access to a private or shared page."},
    {"name": "chat", "description": "Send a message to the ChatAgent orchestrator."},
    {"name": "site", "description": "Provision, inspect, and delete user sites."},
    {"name": "secrets", "description": "Manage per-user credentials stored in AWS Secrets Manager."},
    {"name": "oauth", "description": "OAuth 2.0 + PKCE flow for remote MCP skills."},
    {"name": "token-budget", "description": "Read per-user daily token usage and budget."},
    {"name": "tokens", "description": "Create, list, and revoke site-scoped API tokens."},
    {"name": "obsidian", "description": "Purpose-built sync API for the YoloScribe Obsidian plugin."},
    {"name": "webhooks", "description": "Internal webhooks called by Supabase / external systems, and outbound webhook management."},
    {"name": "mcp", "description": "Remote MCP server for AI coding agents (Claude Code, etc.)."},
]

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="YoloScribe API",
    description=(
        "Backend API for YoloScribe — an AI-powered wiki where every page "
        "can be edited by an LLM agent.\n\n"
        "**Authentication:** most write endpoints require a Supabase JWT passed as "
        "`Authorization: Bearer <token>`.\n\n"
        "**Docs:** interactive Swagger UI is at `/docs`; ReDoc is at `/redoc`."
    ),
    version="1.0.0",
    openapi_tags=_OPENAPI_TAGS,
    lifespan=_lifespan,
)

# ── Global request size guard (YOL-54) ────────────────────────────────────────
# Rejects requests whose Content-Length header exceeds MAX_REQUEST_BYTES before
# the body is read, providing a second line of defence behind per-field limits.


class _RequestSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return JSONResponse(
                        {"detail": f"Request body exceeds maximum allowed size of {MAX_REQUEST_BYTES // 1024} KB"},
                        status_code=413,
                    )
            except ValueError:
                pass
        return await call_next(request)


app.add_middleware(_RequestSizeMiddleware)
app.add_middleware(SlowAPIMiddleware)
_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
_wildcard = "*" in _origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if _wildcard else _origins,
    allow_origin_regex=".*" if _wildcard else None,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
    expose_headers=["X-Page-Access", "ETag", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"],
)

# ── Rate limiter wiring ────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Catch-all: ensures unhandled exceptions return a JSON 500 that passes through
# CORSMiddleware. Without this, Starlette's ServerErrorMiddleware generates a
# plain-text response that bypasses the CORS middleware stack.
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logging.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# ── Mount MCP server ───────────────────────────────────────────────────────────

if LOCAL_MODE or auth_provider is not None:
    _mcp_app = create_mcp_app(  # noqa: F811
        s3_client=s3,
        bucket=S3_BUCKET,
        s3vectors_client=s3vectors,
        vectors_bucket=S3_VECTORS_BUCKET,
        vectors_index=S3_VECTORS_INDEX_NAME,
        bedrock_embedding_model=BEDROCK_EMBEDDING_MODEL,
        bedrock_region=AWS_REGION,
        auth_provider=auth_provider,
        user_site_repo=user_site_repo,
        sqs_indexing_client=sqs_indexing,
        sqs_indexing_queue_url=os.environ.get("SQS_INDEXING_QUEUE_URL", ""),
        base_url=mcp_api_base(),
        local_mode=LOCAL_MODE,
        local_site_name=LOCAL_SITE_NAME,
        local_user_id=LOCAL_USER_ID,
        local_api_key=LOCAL_MCP_API_KEY,
    )
    app.mount("/mcp/v1", _mcp_app)
    logging.info("MCP server mounted at /mcp/v1%s", " (local mode)" if LOCAL_MODE else "")
else:
    logging.warning("MCP server not mounted: auth provider is not configured")

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(health_router)
app.include_router(assets_router)
app.include_router(content_router)
app.include_router(obsidian_router)
app.include_router(pages_router)
app.include_router(settings_router)
app.include_router(chat_router)
app.include_router(tools_router)
app.include_router(oauth_router)
if not LOCAL_MODE:
    app.include_router(mcp_oauth_router)
app.include_router(site_router)
app.include_router(token_budget_router)
app.include_router(tokens_router)
app.include_router(archive_router)
app.include_router(outbound_webhooks_router)
app.include_router(search_router)
app.include_router(versions_router)
app.include_router(webhooks_router)
