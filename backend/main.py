"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import contextlib
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from config import SUPABASE_URL, S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, AWS_REGION, BEDROCK_EMBEDDING_MODEL, mcp_api_base, jwks_client, s3, sqs_indexing, s3vectors
from config import SUPABASE_SERVICE_ROLE_KEY, MAX_REQUEST_BYTES
from mcp_server import create_mcp_app
from routers import (
    chat_router,
    content_router,
    health_router,
    mcp_oauth_router,
    oauth_router,
    pages_router,
    settings_router,
    site_router,
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
    {"name": "webhooks", "description": "Internal webhooks called by Supabase / external systems."},
    {"name": "mcp", "description": "Remote MCP server for AI coding agents (Claude Code, etc.)."},
]

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AgentScribe API",
    description=(
        "Backend API for AgentScribe — an AI-powered wiki where every page "
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Page-Access"],
)

# ── Mount MCP server ───────────────────────────────────────────────────────────

if jwks_client is not None:
    _mcp_app = create_mcp_app(  # noqa: F811
        s3_client=s3,
        bucket=S3_BUCKET,
        s3vectors_client=s3vectors,
        vectors_bucket=S3_VECTORS_BUCKET,
        vectors_index=S3_VECTORS_INDEX_NAME,
        bedrock_embedding_model=BEDROCK_EMBEDDING_MODEL,
        bedrock_region=AWS_REGION,
        jwks_client=jwks_client,
        supabase_url=SUPABASE_URL,
        supabase_service_role_key=SUPABASE_SERVICE_ROLE_KEY,
        sqs_indexing_client=sqs_indexing,
        sqs_indexing_queue_url=os.environ.get("SQS_INDEXING_QUEUE_URL", ""),
        base_url=mcp_api_base(),
    )
    app.mount("/mcp/v1", _mcp_app)
    logging.info("MCP server mounted at /mcp/v1")
else:
    logging.warning("MCP server not mounted: SUPABASE_URL is not configured")

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(health_router)
app.include_router(content_router)
app.include_router(pages_router)
app.include_router(settings_router)
app.include_router(chat_router)
app.include_router(tools_router)
app.include_router(oauth_router)
app.include_router(mcp_oauth_router)
app.include_router(site_router)
app.include_router(webhooks_router)
