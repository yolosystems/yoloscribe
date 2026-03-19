"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import base64
import contextlib
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import boto3
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp_oauth import (
    PKCEChallenge,
    build_authorization_url,
    discover,
    dynamic_client_registration,
)
from mcp_oauth.discovery import AuthorizationServerMetadata
from mcp_oauth.oauth_flow import exchange_code
from pydantic import BaseModel

from agents import ChatAgent
from agents.base import agents_prefix, tools_prefix, skills_prefix
from mcp_server import create_mcp_app

import jwt as pyjwt
from jwt import PyJWKClient

# _mcp_app is set after the FastAPI app is created (it needs _jwks_client).
# The lifespan below delegates to the MCP app's lifespan so FastMCP's internal
# task group is initialised before any requests arrive.
_mcp_app = None


@contextlib.asynccontextmanager
async def _lifespan(app):
    if _mcp_app is not None:
        async with _mcp_app.router.lifespan_context(app):
            yield
    else:
        yield


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Page-Access"],
)

# ── Configuration ─────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "agentscribe")
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")
# In production FRONTEND_URL is derived from CLOUDFRONT_DOMAIN.
# Locally it points to the Vite dev server so the OAuth callback redirect lands correctly.
FRONTEND_URL = (
    f"https://{CLOUDFRONT_DOMAIN}"
    if CLOUDFRONT_DOMAIN
    else os.environ.get("FRONTEND_URL", "http://localhost:5173")
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
_jwks_client = (
    PyJWKClient(
        f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
        cache_keys=True,
        lifespan=600,  # 10 minutes, matching Supabase's edge cache TTL
    )
    if SUPABASE_URL
    else None
)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
EKS_OIDC_PROVIDER = os.environ.get("EKS_OIDC_PROVIDER", "")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "agentscribe")

_aws_profile = os.environ.get("AWS_PROFILE")
_boto_session = boto3.Session(profile_name=_aws_profile) if _aws_profile else boto3.Session()
s3 = _boto_session.client("s3")
sqs = _boto_session.client("sqs", region_name=AWS_REGION) if SQS_QUEUE_URL else None
sqs_indexing = _boto_session.client("sqs", region_name=AWS_REGION) if SQS_INDEXING_QUEUE_URL else None
sm = _boto_session.client("secretsmanager", region_name=AWS_REGION)
s3vectors = _boto_session.client("s3vectors", region_name=AWS_REGION) if S3_VECTORS_BUCKET else None

# ── JWT claims ────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class _JWTClaims:
    user_id: str
    email: str | None


# ── OAuth state (in-memory, TTL ~10 min) ──────────────────────────────────────

@dataclasses.dataclass
class _OAuthPendingState:
    tool_name: str
    user_id: str
    site: str
    server_url: str
    pkce_verifier: str
    client_id: str
    client_secret: str | None
    auth_metadata: dict  # serialized AuthorizationServerMetadata fields
    created_at: float


_oauth_pending: dict[str, _OAuthPendingState] = {}


def _cleanup_oauth_state() -> None:
    cutoff = time.time() - 600
    for k in [k for k, v in _oauth_pending.items() if v.created_at < cutoff]:
        del _oauth_pending[k]


# ── AWS SSO state (in-memory, TTL = expiresIn from StartDeviceAuthorization) ──

@dataclasses.dataclass
class _AwsSsoPendingState:
    user_id: str
    site: str
    sso_region: str
    sso_start_url: str
    aws_region: str
    client_id: str
    client_secret: str
    device_code: str
    created_at: float
    expires_in: int
    interval: int


_aws_sso_pending: dict[str, _AwsSsoPendingState] = {}


def _cleanup_aws_sso_state() -> None:
    cutoff = time.time()
    for k in [k for k, v in _aws_sso_pending.items() if cutoff > v.created_at + v.expires_in]:
        del _aws_sso_pending[k]


# ── MCP OAuth state (in-memory) ────────────────────────────────────────────────

@dataclasses.dataclass
class _McpDcrClient:
    """OAuth client registered by Claude Code via Dynamic Client Registration."""
    client_id: str
    redirect_uris: list[str]
    created_at: float


@dataclasses.dataclass
class _McpAuthPending:
    """State for an in-flight MCP OAuth authorization request."""
    client_id: str
    # Claude Code's PKCE params
    cc_code_challenge: str
    cc_code_challenge_method: str
    cc_redirect_uri: str
    cc_state: str | None
    # AgentScribe→Supabase PKCE verifier (for server-side PKCE exchange)
    supabase_pkce_verifier: str
    created_at: float


@dataclasses.dataclass
class _McpCode:
    """One-time authorization code issued to Claude Code after Supabase callback."""
    client_id: str
    supabase_jwt: str
    supabase_refresh_token: str | None
    cc_code_challenge: str
    cc_code_challenge_method: str
    cc_redirect_uri: str
    cc_state: str | None
    created_at: float


_mcp_dcr_clients: dict[str, _McpDcrClient] = {}
_mcp_auth_pending: dict[str, _McpAuthPending] = {}
_mcp_codes: dict[str, _McpCode] = {}


def _cleanup_mcp_state() -> None:
    cutoff_10m = time.time() - 600
    cutoff_5m = time.time() - 300
    for k in [k for k, v in _mcp_auth_pending.items() if v.created_at < cutoff_10m]:
        del _mcp_auth_pending[k]
    for k in [k for k, v in _mcp_codes.items() if v.created_at < cutoff_5m]:
        del _mcp_codes[k]


def _mcp_api_base() -> str:
    """Derive the public base URL of this server from OAUTH_REDIRECT_URI."""
    return OAUTH_REDIRECT_URI.removesuffix("/oauth/callback")


def _pkce_s256(verifier: str) -> str:
    """Return the S256 code challenge for the given verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── ChatAgent ─────────────────────────────────────────────────────────────────

chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
    sm_client=sm,
)

# ── Remote MCP server ─────────────────────────────────────────────────────────

BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")

if _jwks_client is not None:
    _mcp_app = create_mcp_app(  # noqa: F811 — intentional reassignment of module-level var
        s3_client=s3,
        bucket=S3_BUCKET,
        s3vectors_client=s3vectors,
        vectors_bucket=S3_VECTORS_BUCKET,
        vectors_index=S3_VECTORS_INDEX_NAME,
        bedrock_embedding_model=BEDROCK_EMBEDDING_MODEL,
        bedrock_region=AWS_REGION,
        jwks_client=_jwks_client,
        supabase_url=SUPABASE_URL,
        supabase_service_role_key=SUPABASE_SERVICE_ROLE_KEY,
        sqs_indexing_client=sqs_indexing,
        sqs_indexing_queue_url=SQS_INDEXING_QUEUE_URL,
        base_url=_mcp_api_base(),
    )
    app.mount("/mcp/v1", _mcp_app)
    logging.info("MCP server mounted at /mcp/v1")
else:
    logging.warning("MCP server not mounted: SUPABASE_URL is not configured")

# ── Path safety ───────────────────────────────────────────────────────────────
# Allowed writable paths:
#   content.md
#   {page}/content.md               (child page root content)
#   .agents/{name}/agent.md         (root-page agent definition)
#   {page}/.agents/{name}/agent.md  (child-page agent definition)

AGENT_NAME_SEG = r"[a-z0-9][a-z0-9_-]*"
PAGE_SEG = r"[a-z0-9][a-z0-9_/-]*"

SAFE_PATH = re.compile(
    r"^("
    r"content\.md"
    r"|config\.json"
    r"|settings\.json"
    rf"|{PAGE_SEG}/content\.md"
    rf"|{PAGE_SEG}/settings\.json"
    rf"|\.agents/{AGENT_NAME_SEG}/agent\.md"
    rf"|{PAGE_SEG}/\.agents/{AGENT_NAME_SEG}/agent\.md"
    rf"|\.skills/{AGENT_NAME_SEG}/SKILL\.md"
    r"|\.user/search\.md"
    r"|\.user/notifications\.md"
    r")$"
)

SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
PAGE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
VALID_THEMES = {"light", "dark", "yolo"}


def _is_safe_path(path: str) -> bool:
    return bool(SAFE_PATH.match(path))


def _get_content(site: str, path: str = "content.md") -> str:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/{path}")
        return obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return ""


def _put_content(site: str, path: str, content: str) -> None:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/{path}",
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def _delete_s3_prefix(site_name: str) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": objects, "Quiet": True})


def _delete_site_vectors(site_name: str) -> None:
    """Delete all S3 Vectors entries for a site (best-effort, never raises).

    Chunk objects live at {site}/.chunks/{uuid} and at
    {site}/{page}/.chunks/{uuid} (and any depth of child pages).
    A single list_objects_v2 over the site prefix (no delimiter) returns
    everything recursively, so we just filter for keys containing "/.chunks/".
    The vector key for each chunk is the UUID — the final path segment.
    """
    if s3vectors is None:
        return
    paginator = s3.get_paginator("list_objects_v2")
    vector_ids: list[str] = []
    try:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
            for obj in page.get("Contents", []):
                if "/.chunks/" in obj["Key"]:
                    vector_ids.append(obj["Key"].split("/")[-1])
    except Exception as exc:
        logging.warning("Failed to list chunks for vector deletion (%s): %s", site_name, exc)
        return
    if not vector_ids:
        return
    try:
        for i in range(0, len(vector_ids), 100):
            s3vectors.delete_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                keys=vector_ids[i : i + 100],
            )
        logging.info("Deleted %d vectors for site %s", len(vector_ids), site_name)
    except Exception as exc:
        logging.warning("Failed to delete vectors for site %s: %s", site_name, exc)


def _enqueue_index_job(content_key: str, user_id: str) -> None:
    """Send an indexing job to the SQS indexing queue (best-effort; never raises)."""
    if sqs_indexing is None or not SQS_INDEXING_QUEUE_URL:
        return
    try:
        sqs_indexing.send_message(
            QueueUrl=SQS_INDEXING_QUEUE_URL,
            MessageBody=json.dumps({"bucket": S3_BUCKET, "content_key": content_key, "user_id": user_id}),
        )
    except Exception:
        logging.warning("Failed to enqueue indexing job for %s", content_key, exc_info=True)


# ── Page settings cache ───────────────────────────────────────────────────────

_settings_cache: dict[str, tuple[dict, float]] = {}
_settings_cache_lock = threading.Lock()
_SETTINGS_CACHE_TTL = 60.0  # seconds


def _page_path_from_file_path(path: str) -> str:
    """Return the page_path (S3 prefix segment) for a given file path.

    Examples:
        "content.md"              → ""
        "blog/content.md"         → "blog"
        "blog/posts/content.md"   → "blog/posts"
        "settings.json"           → ""
        "blog/settings.json"      → "blog"
        ".agents/foo/agent.md"    → ""
        "blog/.agents/foo/agent.md" → "blog"
    """
    if "/" not in path:
        return ""
    # Strip the final segment(s) that are meta-files
    for suffix in ("/content.md", "/settings.json"):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    agents_idx = path.find("/.agents/")
    if agents_idx != -1:
        return path[:agents_idx]
    return ""


def _get_page_settings(site: str, page_path: str) -> dict:
    """Return parsed settings.json for a page (with in-memory TTL cache)."""
    cache_key = f"{site}/{page_path}"
    now = time.time()
    with _settings_cache_lock:
        if cache_key in _settings_cache:
            data, ts = _settings_cache[cache_key]
            if now - ts < _SETTINGS_CACHE_TTL:
                return data
    s3_path = "settings.json" if not page_path else f"{page_path}/settings.json"
    raw = _get_content(site, s3_path)
    data: dict = json.loads(raw) if raw else {"visibility": "private", "shared_with": []}
    with _settings_cache_lock:
        _settings_cache[cache_key] = (data, now)
    return data


def _invalidate_settings_cache(site: str, page_path: str) -> None:
    cache_key = f"{site}/{page_path}"
    with _settings_cache_lock:
        _settings_cache.pop(cache_key, None)


# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _decode_jwt(credentials: HTTPAuthorizationCredentials | None) -> _JWTClaims:
    """Validate Supabase JWT and return user_id + email."""
    if _jwks_client is None:
        raise HTTPException(status_code=500, detail="SUPABASE_URL is not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    token = credentials.credentials
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
        )
        return _JWTClaims(user_id=payload["sub"], email=payload.get("email"))
    except pyjwt.exceptions.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc


def _get_site_for_user(user_id: str) -> str | None:
    """Look up the user's site name from the user_site table via Supabase PostgREST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/user_site?user_uuid=eq.{user_id}&select=site_name&limit=1"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            return data[0]["site_name"] if data else None
    except Exception:
        return None


def _get_user_id(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> str:
    """Extract user_id from JWT (backwards-compatible for /secrets routes)."""
    return _decode_jwt(credentials).user_id


def _get_user_context(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> tuple[str, str | None]:
    """Extract user_id from JWT and look up site_name from user_site table."""
    claims = _decode_jwt(credentials)
    return claims.user_id, _get_site_for_user(claims.user_id)


def _get_jwt_claims(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> _JWTClaims:
    """Extract and validate JWT, returning full claims including email."""
    return _decode_jwt(credentials)


def _require_site_owner(requested_site: str, user_site: str | None) -> None:
    if user_site is None or user_site != requested_site:
        raise HTTPException(status_code=403, detail="Access denied: not your site")


# ── Supabase admin helpers ────────────────────────────────────────────────────

def _supabase_insert_user_site(user_id: str, site_name: str, theme: str) -> None:
    """Insert into user_site table via Supabase PostgREST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    url = f"{SUPABASE_URL}/rest/v1/user_site"
    data = json.dumps({"user_uuid": user_id, "site_name": site_name, "theme": theme}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase PostgREST error: {exc}") from exc


def _supabase_delete_user_site(user_id: str) -> None:
    """Delete from user_site table via Supabase PostgREST. Logs warning on failure, does not raise."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/user_site?user_uuid=eq.{user_id}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        urllib.request.urlopen(req)
    except Exception as exc:
        logging.warning("Failed to delete user_site row for %s: %s", user_id, exc)


def _supabase_delete_auth_user(user_id: str) -> None:
    """Delete Supabase Auth user. Raises HTTPException(502) on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    url = f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase Auth delete error: {exc}") from exc


# ── Infrastructure provisioning ───────────────────────────────────────────────

async def _provision_user_infrastructure(user_id: str, site_name: str) -> None:
    """Provision IAM role, K8s ServiceAccount, and SM placeholder for a new user."""
    role_name = f"agentscribe-user-{user_id}"
    sa_name = f"user-{user_id}"
    sm_secret_name = f"agentscribe/{user_id}/.initialized"

    iam = _boto_session.client("iam")
    secrets_manager = _boto_session.client("secretsmanager", region_name=AWS_REGION)

    # 1. Create IAM role with IRSA trust policy
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": f"arn:aws:iam::{AWS_ACCOUNT_ID}:oidc-provider/{EKS_OIDC_PROVIDER}"
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{EKS_OIDC_PROVIDER}:sub": f"system:serviceaccount:{K8S_NAMESPACE}:{sa_name}",
                        f"{EKS_OIDC_PROVIDER}:aud": "sts.amazonaws.com",
                    }
                },
            }
        ],
    }
    iam.create_role(
        RoleName=role_name,
        Path="/agentscribe/",
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description=f"IRSA role for AgentScribe user {user_id}",
    )

    # 2. Attach inline policy: allow reading secrets + scoped S3 access for this user's site
    secret_arn_prefix = (
        f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:agentscribe/{user_id}/"
    )
    s3_bucket_arn = f"arn:aws:s3:::{S3_BUCKET}"
    statements: list[dict] = [
        {
            "Sid": "SecretsManagerUserSecrets",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
            ],
            "Resource": f"{secret_arn_prefix}*",
        },
        {
            "Sid": "S3ReadWriteUserPrefix",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": f"{s3_bucket_arn}/{site_name}/*",
        },
        {
            "Sid": "S3ReadToolsPrefix",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": f"{s3_bucket_arn}/.tools/*",
        },
        {
            "Sid": "S3ListUserPrefix",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": s3_bucket_arn,
            "Condition": {
                "StringLike": {"s3:prefix": [f"{site_name}/*", ".tools/*"]}
            },
        },
    ]
    if SQS_INDEXING_QUEUE_URL:
        queue_name = SQS_INDEXING_QUEUE_URL.rstrip("/").split("/")[-1]
        indexing_queue_arn = f"arn:aws:sqs:{AWS_REGION}:{AWS_ACCOUNT_ID}:{queue_name}"
        statements.append(
            {
                "Sid": "SQSSendIndexingQueue",
                "Effect": "Allow",
                "Action": "sqs:SendMessage",
                "Resource": indexing_queue_arn,
            }
        )
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="agentscribe-user-access",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    role_arn = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/agentscribe/{role_name}"

    # 3. Create K8s ServiceAccount annotated with role ARN
    try:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]

        kubeconfig = os.environ.get("KUBECONFIG")
        if kubeconfig:
            k8s_config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()
        sa = k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                name=sa_name,
                namespace=K8S_NAMESPACE,
                annotations={"eks.amazonaws.com/role-arn": role_arn},
            )
        )
        v1.create_namespaced_service_account(namespace=K8S_NAMESPACE, body=sa)
    except Exception as k8s_exc:
        raise HTTPException(
            status_code=502, detail=f"K8s ServiceAccount creation failed: {k8s_exc}"
        ) from k8s_exc

    # 4. Create Secrets Manager placeholder
    secrets_manager.create_secret(
        Name=sm_secret_name,
        SecretString=json.dumps({"initialized": "true"}),
        Description=f"Placeholder secret for AgentScribe user {user_id}",
    )


async def _deprovision_user_infrastructure(user_id: str, site_name: str | None) -> list[str]:
    """Delete IAM role/policy, SM secrets, and K8s ServiceAccount for a user.

    Returns a list of warning strings. Never raises.
    """
    warnings: list[str] = []
    role_name = f"agentscribe-user-{user_id}"
    sa_name = f"user-{user_id}"

    iam = _boto_session.client("iam")
    secrets_manager = _boto_session.client("secretsmanager", region_name=AWS_REGION)

    # 1. Delete IAM inline policy
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName="agentscribe-user-access")
    except iam.exceptions.NoSuchEntityException:
        pass
    except Exception as exc:
        warnings.append(f"IAM policy delete warning: {exc}")

    # 2. Delete IAM role
    try:
        iam.delete_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        pass
    except Exception as exc:
        warnings.append(f"IAM role delete warning: {exc}")

    # 3. Delete SM secrets under agentscribe/{user_id}/
    prefix = f"agentscribe/{user_id}/"
    try:
        paginator = secrets_manager.get_paginator("list_secrets")
        for page in paginator.paginate():
            for secret in page.get("SecretList", []):
                if secret["Name"].startswith(prefix):
                    try:
                        secrets_manager.delete_secret(
                            SecretId=secret["ARN"],
                            ForceDeleteWithoutRecovery=True,
                        )
                    except Exception as exc:
                        warnings.append(f"SM secret delete warning ({secret['Name']}): {exc}")
    except Exception as exc:
        warnings.append(f"SM list secrets warning: {exc}")

    # 4. Delete K8s ServiceAccount
    try:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]

        kubeconfig = os.environ.get("KUBECONFIG")
        if kubeconfig:
            k8s_config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()
        try:
            v1.delete_namespaced_service_account(name=sa_name, namespace=K8S_NAMESPACE)
        except Exception as exc:
            if "404" not in str(exc) and "Not Found" not in str(exc):
                warnings.append(f"K8s ServiceAccount delete warning: {exc}")
    except Exception as k8s_exc:
        warnings.append(f"K8s config warning: {k8s_exc}")

    for w in warnings:
        logging.warning(w)
    return warnings


# ── Request / response models ─────────────────────────────────────────────────


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    current_content: str
    history: list[HistoryMessage] = []
    site: str = "default"
    file_path: str = "content.md"


class ChatResponse(BaseModel):
    reply: str
    updated_content: str | None = None
    navigate_to: str | None = None


class UserCreatedEvent(BaseModel):
    user_id: str


class SecretValue(BaseModel):
    value: str


class ProvisionRequest(BaseModel):
    site_name: str
    theme: str


class ProvisionResponse(BaseModel):
    site_url: str


class CreatePageRequest(BaseModel):
    site: str
    page_path: str


class SharedUser(BaseModel):
    email: str
    access: str  # "view" | "write"


class PageSettings(BaseModel):
    visibility: str  # "public" | "private" | "shared"
    shared_with: list[SharedUser] = []


class AccessRequest(BaseModel):
    site: str
    path: str


# ── Default welcome content ───────────────────────────────────────────────────

_DEFAULT_WELCOME_MD = """\
# Welcome to your AgentScribe site!

This is the home page of your personal wiki. Edit this content using the editor,
or ask the AI assistant in the Chat panel to help you write and organise your notes.

## Getting Started

- Click **Edit** to enter edit mode
- Use the **Chat** panel to ask the AI to help you write content
- Navigate to sub-pages by clicking links
"""


def _default_child_page_md(title: str) -> str:
    return (
        f"# {title}\n\n"
        f"This is a new wiki page. Edit this content using the editor,\n"
        f"or ask the AI assistant in the Chat panel to help you write and organise your notes.\n\n"
        f"## Getting Started\n\n"
        f"- Click **Edit** to enter edit mode\n"
        f"- Use the **Chat** panel to ask the AI to help you write content\n"
        f"- Navigate to sub-pages by clicking links\n"
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["health"], summary="Health check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/content",
    tags=["content"],
    summary="Get page content",
    description=(
        "Return the raw content of a page file from S3. Visibility rules apply: "
        "public pages are readable without auth; private/shared pages require a JWT. "
        "The `X-Page-Access` response header indicates the caller's access level "
        "(`full-control` | `write` | `view`)."
    ),
)
async def get_content(
    site: str = "default",
    path: str = "content.md",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Determine the page_path for settings lookup
    page_path = _page_path_from_file_path(path)
    is_content_path = path == "content.md" or path.endswith("/content.md")

    # config.json is always public — the frontend reads it unauthenticated to set the theme
    if path == "config.json":
        content = _get_content(site, path)
        return Response(content=content, media_type="application/json")

    # Non-content paths (agents, settings, notifications, search) are always owner-only
    if not is_content_path:
        claims = _decode_jwt(credentials)
        user_site = _get_site_for_user(claims.user_id)
        _require_site_owner(site, user_site)
        content = _get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        return resp

    # Content pages: check visibility settings
    settings = _get_page_settings(site, page_path)
    visibility = settings.get("visibility", "private")

    if visibility == "public":
        access = "view"
        if credentials is not None:
            try:
                claims = _decode_jwt(credentials)
                user_site = _get_site_for_user(claims.user_id)
                if user_site == site:
                    access = "full-control"
            except HTTPException:
                pass  # invalid token — serve as public view
        content = _get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = access
        return resp

    # private or shared — authentication required
    if credentials is None:
        raise HTTPException(status_code=403, detail="Authentication required")

    claims = _decode_jwt(credentials)
    user_site = _get_site_for_user(claims.user_id)

    if user_site == site:
        content = _get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        return resp

    if visibility == "shared":
        user_email = claims.email
        shared_with = settings.get("shared_with", [])
        match = next((u for u in shared_with if u.get("email") == user_email), None)
        if match:
            content = _get_content(site, path)
            resp = Response(content=content, media_type="text/plain; charset=utf-8")
            resp.headers["X-Page-Access"] = match.get("access", "view")
            return resp

    raise HTTPException(status_code=403, detail="Access denied")


@app.put(
    "/content",
    tags=["content"],
    summary="Update page content",
    description=(
        "Write raw content to a page file in S3. Requires a JWT. "
        "Site owners can write any allowed path; shared-write users may only update `content.md`."
    ),
)
async def put_content(
    request: Request,
    site: str = "default",
    path: str = "content.md",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict[str, str]:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")

    claims = _decode_jwt(credentials)
    user_site = _get_site_for_user(claims.user_id)

    is_content_path = path == "content.md" or path.endswith("/content.md")

    if user_site == site:
        # Site owner: full access
        pass
    elif is_content_path:
        # Non-owner may write content.md if they have shared "write" access for this page
        page_path = _page_path_from_file_path(path)
        settings = _get_page_settings(site, page_path)
        if settings.get("visibility") != "shared":
            raise HTTPException(status_code=403, detail="Access denied")
        shared_with = settings.get("shared_with", [])
        match = next((u for u in shared_with if u.get("email") == claims.email), None)
        if not match or match.get("access") != "write":
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        # Non-owner cannot write agent definitions or other meta-files
        raise HTTPException(status_code=403, detail="Access denied: not your site")

    body = await request.body()
    _put_content(site, path, body.decode("utf-8"))
    if is_content_path:
        _enqueue_index_job(f"{site}/{path}", claims.user_id)
    return {"status": "saved"}


@app.get("/agents", tags=["agents"], summary="List agents for a page")
async def list_agents(site: str = "default", page_path: str = "") -> dict:
    prefix = agents_prefix(site, page_path)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"agents": names}


@app.get("/pages", tags=["pages"], summary="List child pages")
async def list_pages(site: str = "default", page_path: str = "") -> dict:
    prefix = f"{site}/{page_path}/" if page_path else f"{site}/"
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/")
    pages = []
    for cp in resp.get("CommonPrefixes", []):
        name = cp["Prefix"][len(prefix):].rstrip("/")
        if name.startswith("."):
            continue
        content_key = f"{cp['Prefix']}content.md"
        check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
        if check.get("KeyCount", 0) > 0:
            pages.append(name)
    return {"pages": pages}


@app.post("/pages", tags=["pages"], summary="Create a new page")
async def create_page(
    req: CreatePageRequest,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    user_id, user_site = ctx
    _require_site_owner(req.site, user_site)

    if not PAGE_PATH_RE.match(req.page_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid page path: use lowercase letters, digits, hyphens, and underscores separated by slashes",
        )

    content_key = f"{req.site}/{req.page_path}/content.md"
    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
    if check.get("KeyCount", 0) > 0:
        raise HTTPException(status_code=409, detail="Page already exists")

    title = req.page_path.split("/")[-1].replace("-", " ").title()
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=content_key,
        Body=_default_child_page_md(title).encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site}/{req.page_path}/.agents/.keep",
        Body=b"",
        ContentType="text/plain",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site}/{req.page_path}/settings.json",
        Body=json.dumps({"visibility": "private", "shared_with": []}).encode("utf-8"),
        ContentType="application/json",
    )
    _enqueue_index_job(content_key, user_id)
    return {"page_path": req.page_path, "content_key": content_key}


@app.get("/skills", tags=["skills"], summary="List skills for a site")
async def list_skills(
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return all skill names defined in the site's .skills directory."""
    user_id, user_site = ctx
    _require_site_owner(site, user_site)
    prefix = skills_prefix(site)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"skills": names}


@app.get("/settings", tags=["settings"], summary="Get page access-control settings")
async def get_settings(
    site: str = "default",
    path: str = "content.md",
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return access-control settings for a page (site owner only)."""
    user_id, user_site = ctx
    _require_site_owner(site, user_site)
    page_path = _page_path_from_file_path(path)
    return _get_page_settings(site, page_path)


@app.put("/settings", tags=["settings"], summary="Update page access-control settings")
async def put_settings(
    settings: PageSettings,
    site: str = "default",
    path: str = "content.md",
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict[str, str]:
    """Update access-control settings for a page (site owner only)."""
    user_id, user_site = ctx
    _require_site_owner(site, user_site)

    valid_visibilities = {"public", "private", "shared"}
    if settings.visibility not in valid_visibilities:
        raise HTTPException(status_code=400, detail=f"visibility must be one of: {', '.join(sorted(valid_visibilities))}")
    valid_accesses = {"view", "write"}
    for su in settings.shared_with:
        if su.access not in valid_accesses:
            raise HTTPException(status_code=400, detail=f"shared_with access must be 'view' or 'write'")

    page_path = _page_path_from_file_path(path)
    s3_path = "settings.json" if not page_path else f"{page_path}/settings.json"
    _put_content(site, s3_path, json.dumps(settings.model_dump()))
    _invalidate_settings_cache(site, page_path)
    return {"status": "saved"}


@app.post("/request-access", tags=["access"], summary="Request access to a page")
async def request_access(
    req: AccessRequest,
    claims: _JWTClaims = Depends(_get_jwt_claims),
) -> dict[str, str]:
    """Append an access-request notification to the site owner's notifications file."""
    if not req.site or not req.path:
        raise HTTPException(status_code=400, detail="site and path are required")

    # Verify the page exists
    page_path = _page_path_from_file_path(req.path)
    content_key = f"{req.site}/{req.path}"
    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
    if check.get("KeyCount", 0) == 0:
        raise HTTPException(status_code=404, detail="Page not found")

    # Check user doesn't already have access (avoid notification spam)
    settings = _get_page_settings(req.site, page_path)
    if claims.email:
        already_shared = any(
            u.get("email") == claims.email for u in settings.get("shared_with", [])
        )
        if already_shared:
            raise HTTPException(status_code=409, detail="You already have access to this page")

    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    requester = claims.email or claims.user_id
    entry = f"- [{ts}] **{requester}** requested access to `{req.path}`\n"

    existing = _get_content(req.site, ".user/notifications.md")
    _put_content(req.site, ".user/notifications.md", existing + entry)
    return {"status": "ok"}


# ── Secrets (credentials) — no LLM involved ───────────────────────────────────

_SM_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SM_SECRET_PREFIX = "agentscribe"


def _secret_id(user_id: str, var_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/{var_name}"


def _oauth_secret_id(user_id: str, tool_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/oauth/{tool_name}"


def _is_remote_tool(tool_name: str) -> bool:
    """Return True if the tool's mcp.json uses remote HTTP transport."""
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        config = json.loads(obj["Body"].read())
        return any("url" in srv for srv in config.get("mcpServers", {}).values())
    except Exception:
        return False


def _tool_required_vars(tool_name: str) -> list[str]:
    """Read a stdio tool's mcp.json from S3 and return required ${VAR} names."""
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        raw = obj["Body"].read().decode("utf-8")
        return list(dict.fromkeys(_SM_VAR_RE.findall(raw)))
    except Exception:
        return []


def _secret_exists(user_id: str, var_name: str) -> bool:
    try:
        sm.get_secret_value(SecretId=_secret_id(user_id, var_name))
        return True
    except sm.exceptions.ResourceNotFoundException:
        return False
    except Exception:
        return False


def _load_tool_oauth_client(tool_name: str) -> dict | None:
    """Load pre-registered OAuth client config from S3 for a tool.

    Returns the parsed oauth_client.json dict, or None if the file does not exist.
    This file is present for tools whose MCP servers do not support DCR (e.g. GitHub).
    Format: {"client_id": "...", "scopes": ["repo", ...]}  (scopes is optional)
    The corresponding client_secret is stored in Secrets Manager at
    agentscribe/platform/oauth/{tool_name}.
    """
    key = f"{tools_prefix()}/{tool_name}/oauth_client.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _get_tool_auth_type(tool_name: str) -> str:
    """Return the auth type from a tool's mcp.json: 'oauth', 'aws-sso', 'none', or 'key'.

    'key'  — stdio tool using ${VAR} substitution (no URL)
    'none' — remote HTTP tool with no authentication
    'oauth'— remote HTTP tool using standard OAuth 2.0
    'aws-sso' — remote HTTP tool using AWS IAM Identity Center SSO
    """
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        config = json.loads(obj["Body"].read())
        for srv in config.get("mcpServers", {}).values():
            if "url" in srv:
                return srv.get("auth", "none")
        return "key"
    except Exception:
        return "key"


def _get_aws_sso_client_config(site: str) -> dict | None:
    """Read {site}/.aws-sso/aws-sso-client.json from S3, or None if absent."""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/.aws-sso/aws-sso-client.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None


def _save_aws_sso_client_config(site: str, config: dict) -> None:
    """Write {site}/.aws-sso/aws-sso-client.json to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/.aws-sso/aws-sso-client.json",
        Body=json.dumps(config).encode("utf-8"),
        ContentType="application/json",
    )


def _load_platform_client_secret(tool_name: str) -> str | None:
    """Load the platform-level OAuth client_secret from Secrets Manager.

    Secret path: agentscribe/platform/oauth/{tool_name}
    Expected format: {"client_secret": "..."}
    The backend IAM role must have secretsmanager:GetSecretValue on this path.
    Returns None if the secret does not exist or cannot be read.
    """
    secret_id = f"agentscribe/platform/oauth/{tool_name}"
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        data = json.loads(resp["SecretString"])
        return data.get("client_secret")
    except Exception:
        return None


def _load_oauth_token(user_id: str, tool_name: str) -> dict | None:
    """Load a stored OAuth token blob from Secrets Manager, or None if not found."""
    try:
        resp = sm.get_secret_value(SecretId=_oauth_secret_id(user_id, tool_name))
        return json.loads(resp["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        return None
    except Exception:
        return None


def _save_oauth_token(user_id: str, tool_name: str, token_blob: dict) -> None:
    """Create or update the OAuth token blob in Secrets Manager."""
    secret_id = _oauth_secret_id(user_id, tool_name)
    secret_string = json.dumps(token_blob)
    try:
        sm.put_secret_value(SecretId=secret_id, SecretString=secret_string)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=secret_id,
            SecretString=secret_string,
            Description=f"OAuth tokens for user {user_id} tool {tool_name}",
        )


# ── User settings (enabled tools) ────────────────────────────────────────────


def _get_user_settings(site: str) -> dict:
    """Read {site}/.user/settings.json from S3; return {} if absent."""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/.user/settings.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception:
        return {}


def _save_user_settings(site: str, settings: dict) -> None:
    """Write {site}/.user/settings.json to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/.user/settings.json",
        Body=json.dumps(settings).encode("utf-8"),
        ContentType="application/json",
    )


@app.get("/tools", tags=["tools"], summary="List all tools with per-user status")
async def get_tools(ctx: tuple[str, str | None] = Depends(_get_user_context)) -> dict:
    """Return all tools with enabled state and credential status for this user.

    Remote OAuth tools return: {type: "oauth", enabled, authenticated, expires_at, scope}
    Stdio key-based tools return: {type: "key", enabled, vars, stored}
    """
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    prefix = tools_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    tool_names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    settings = _get_user_settings(user_site)
    enabled_tools: list[str] = settings.get("enabled_tools", [])

    # Load the shared AWS SSO token once — all aws-sso tools share it.
    _aws_sso_token: dict | None = None
    _aws_sso_token_loaded = False
    _aws_sso_config: dict | None = None
    _aws_sso_config_loaded = False

    def _get_aws_sso_token() -> dict | None:
        nonlocal _aws_sso_token, _aws_sso_token_loaded
        if not _aws_sso_token_loaded:
            _aws_sso_token = _load_oauth_token(user_id, "aws-sso")
            _aws_sso_token_loaded = True
        return _aws_sso_token

    def _get_sso_config() -> tuple[bool, str | None, str | None]:
        nonlocal _aws_sso_config, _aws_sso_config_loaded
        if not _aws_sso_config_loaded:
            _aws_sso_config = _get_aws_sso_client_config(user_site or "")
            _aws_sso_config_loaded = True
        if _aws_sso_config and _aws_sso_config.get("sso_start_url"):
            sso_r = _aws_sso_config.get("sso_region", "us-east-1")
            return (
                True,
                _aws_sso_config.get("sso_start_url"),
                sso_r,
                _aws_sso_config.get("aws_region", sso_r),
            )
        return False, None, None, None

    tools_out: dict = {}
    for tool_name in tool_names:
        enabled = tool_name in enabled_tools
        auth_type = _get_tool_auth_type(tool_name)

        if auth_type == "aws-sso":
            token = _get_aws_sso_token() if enabled else None
            configured, sso_start_url_val, sso_region_val, aws_region_val = _get_sso_config()
            if enabled and token:
                expires_at = token.get("expires_at")
                expires_str = (
                    datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
                    if expires_at else None
                )
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": True,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": True,
                    "account_id": token.get("account_id"),
                    "role_name": token.get("role_name"),
                    "expires_at": expires_str,
                }
            elif enabled:
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": True,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": False,
                    "account_id": None,
                    "role_name": None,
                    "expires_at": None,
                }
            else:
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": False,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": False,
                    "account_id": None,
                    "role_name": None,
                    "expires_at": None,
                }

        elif auth_type in ("oauth", "none"):
            if enabled:
                token = _load_oauth_token(user_id, tool_name)
                if token:
                    expires_at = token.get("expires_at")
                    expires_str = (
                        datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
                        if expires_at else None
                    )
                    tools_out[tool_name] = {
                        "type": "oauth",
                        "enabled": True,
                        "authenticated": True,
                        "expires_at": expires_str,
                        "scope": token.get("scope") or None,
                    }
                else:
                    tools_out[tool_name] = {
                        "type": "oauth",
                        "enabled": True,
                        "authenticated": False,
                        "expires_at": None,
                        "scope": None,
                    }
            else:
                tools_out[tool_name] = {
                    "type": "oauth",
                    "enabled": False,
                    "authenticated": False,
                    "expires_at": None,
                    "scope": None,
                }

        else:  # "key" — stdio tool
            if enabled:
                vars_needed = _tool_required_vars(tool_name)
                stored = {v: _secret_exists(user_id, v) for v in vars_needed}
                tools_out[tool_name] = {"type": "key", "enabled": True, "vars": vars_needed, "stored": stored}
            else:
                tools_out[tool_name] = {"type": "key", "enabled": False, "vars": [], "stored": {}}

    return {"tools": tools_out}


@app.post("/tools/{tool_name}/enable", tags=["tools"], summary="Enable a tool for the current user")
async def enable_tool(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict[str, str]:
    """Add tool_name to the user's enabled_tools list in their site settings."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")
    settings = _get_user_settings(user_site)
    enabled: list[str] = settings.get("enabled_tools", [])
    if tool_name not in enabled:
        enabled.append(tool_name)
        settings["enabled_tools"] = enabled
        _save_user_settings(user_site, settings)
    return {"status": "enabled"}


@app.post("/tools/{tool_name}/disable", tags=["tools"], summary="Disable a tool for the current user")
async def disable_tool(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict[str, str]:
    """Remove tool_name from the user's enabled_tools list and delete stored credentials."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    # Update settings
    settings = _get_user_settings(user_site)
    enabled: list[str] = settings.get("enabled_tools", [])
    if tool_name in enabled:
        enabled.remove(tool_name)
        settings["enabled_tools"] = enabled
        _save_user_settings(user_site, settings)

    # Delete stored OAuth token (best-effort).
    # aws-sso tokens are shared across all aws-sso tools so we don't delete them
    # when a single tool is disabled — the user must re-authenticate explicitly.
    if _get_tool_auth_type(tool_name) not in ("aws-sso",):
        try:
            secret_id = _oauth_secret_id(user_id, tool_name)
            sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            logging.warning("Failed to delete OAuth token for tool %s user %s: %s", tool_name, user_id, exc)

    # Delete stored key-based vars (best-effort)
    for var_name in _tool_required_vars(tool_name):
        try:
            sm.delete_secret(SecretId=_secret_id(user_id, var_name), ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            logging.warning("Failed to delete secret %s for user %s: %s", var_name, user_id, exc)

    return {"status": "disabled"}


@app.get("/secrets/status", tags=["secrets"], summary="Get credential status for all tools (legacy alias)")
async def get_secrets_status(ctx: tuple[str, str | None] = Depends(_get_user_context)) -> dict:
    """Return all tools with their credential status for this user.

    This is an alias for GET /tools kept for backwards compatibility.
    """
    return await get_tools(ctx)


@app.post("/oauth/initiate/{tool_name}", tags=["oauth"], summary="Initiate OAuth flow for a tool")
async def oauth_initiate(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Begin the OAuth flow for a remote MCP tool.

    Performs OAuth discovery, then either:
    - Uses a pre-registered client from .tools/{tool_name}/oauth_client.json in S3
      (for servers like GitHub that don't support Dynamic Client Registration), or
    - Performs Dynamic Client Registration (RFC 7591) for servers that support it.

    Returns an authorization URL for the frontend to redirect the browser to.
    """
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    # AWS SSO uses a completely different flow — device authorization grant.
    if tool_name == "aws-sso":
        return await _initiate_aws_sso(user_id, user_site)

    # Load the tool's mcp.json and extract the remote server URL
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        mcp_config = json.loads(obj["Body"].read())
    except Exception:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found or has no mcp.json")

    server_url: str | None = None
    for server_cfg in mcp_config.get("mcpServers", {}).values():
        if "url" in server_cfg:
            server_url = server_cfg["url"]
            break
    if not server_url:
        raise HTTPException(status_code=400, detail=f"Tool '{tool_name}' is not a remote MCP tool")

    _cleanup_oauth_state()

    # Check for a pre-registered OAuth client (tools whose servers don't support DCR).
    # oauth_client.json lives in S3 at .tools/{tool_name}/oauth_client.json and is
    # managed by the platform operator (not per-user).
    pre_registered = _load_tool_oauth_client(tool_name)

    # Discover OAuth metadata.
    # For pre-registered clients whose oauth_client.json already contains
    # authorization_endpoint + token_endpoint (e.g. Google Workspace running
    # behind an internal K8s URL that can't serve well-known endpoints), we skip
    # network discovery entirely and build the metadata from the stored values.
    auth_meta: AuthorizationServerMetadata | None = None
    if pre_registered and pre_registered.get("authorization_endpoint") and pre_registered.get("token_endpoint"):
        auth_meta = AuthorizationServerMetadata(
            issuer=pre_registered.get("issuer", pre_registered["authorization_endpoint"]),
            authorization_endpoint=pre_registered["authorization_endpoint"],
            token_endpoint=pre_registered["token_endpoint"],
            registration_endpoint=pre_registered.get("registration_endpoint"),
            scopes_supported=pre_registered.get("scopes", []),
            code_challenge_methods_supported=pre_registered.get("code_challenge_methods_supported", ["S256"]),
        )
    else:
        try:
            _prm, auth_meta = await discover(server_url)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OAuth discovery failed for {server_url}: {exc}") from exc
        if auth_meta is None:
            raise HTTPException(status_code=502, detail=f"No OAuth authorization server found for {server_url}")

    if pre_registered:
        # Use the platform-registered client — no DCR needed.
        client_id: str = pre_registered["client_id"]
        client_secret: str | None = _load_platform_client_secret(tool_name)
        # oauth_client.json scopes take priority; fall back to server-advertised scopes.
        scopes: list[str] = pre_registered.get("scopes") or (
            list(auth_meta.scopes_supported) if auth_meta.scopes_supported else []
        )
    else:
        # Dynamic Client Registration (per-user client, servers that support RFC 7591).
        if not auth_meta.registration_endpoint:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Tool '{tool_name}' has no pre-registered OAuth client "
                    f"and the MCP server at {server_url} does not support "
                    f"Dynamic Client Registration. Upload an oauth_client.json "
                    f"to .tools/{tool_name}/ in S3 with the pre-registered client_id."
                ),
            )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                client_data = await dynamic_client_registration(
                    client,
                    auth_meta.registration_endpoint,
                    OAUTH_REDIRECT_URI,
                    "AgentScribe",
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Dynamic client registration failed: {exc}") from exc

        client_id = client_data["client_id"]
        client_secret = client_data.get("client_secret")
        scopes = list(auth_meta.scopes_supported) if auth_meta.scopes_supported else []

    # PKCE + state
    pkce = PKCEChallenge()
    state = secrets.token_urlsafe(32)

    auth_url = build_authorization_url(
        metadata=auth_meta,
        client_id=client_id,
        redirect_uri=OAUTH_REDIRECT_URI,
        pkce=pkce,
        state=state,
        scopes=scopes,
        resource=server_url,
    )

    _oauth_pending[state] = _OAuthPendingState(
        tool_name=tool_name,
        user_id=user_id,
        site=user_site,
        server_url=server_url,
        pkce_verifier=pkce.verifier,
        client_id=client_id,
        client_secret=client_secret,
        auth_metadata={
            "issuer": auth_meta.issuer,
            "authorization_endpoint": auth_meta.authorization_endpoint,
            "token_endpoint": auth_meta.token_endpoint,
            "registration_endpoint": auth_meta.registration_endpoint,
            "scopes_supported": auth_meta.scopes_supported,
            "code_challenge_methods_supported": auth_meta.code_challenge_methods_supported,
        },
        created_at=time.time(),
    )

    return {"auth_url": auth_url}


@app.get("/oauth/callback", tags=["oauth"], summary="OAuth authorization code callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Receive the OAuth authorization code callback and exchange it for tokens.

    Stores the token in Secrets Manager, then redirects the browser back to the
    frontend with ?oauth_success={skill_name} or ?oauth_error={message}.
    """
    _cleanup_oauth_state()

    def _frontend_redirect(site: str, params: str) -> RedirectResponse:
        return RedirectResponse(url=f"{FRONTEND_URL}/{site}?{params}", status_code=302)

    if error:
        pending = _oauth_pending.get(state or "")
        site = pending.site if pending else "default"
        return _frontend_redirect(site, f"oauth_error={error_description or error}")

    if not state or state not in _oauth_pending:
        return Response(  # type: ignore[return-value]
            content="Invalid or expired OAuth state. Please try authenticating again.",
            status_code=400,
        )

    pending = _oauth_pending.pop(state)

    # Reconstruct the metadata and a minimal PKCE object (verifier only needed for exchange)
    auth_meta = AuthorizationServerMetadata(
        issuer=pending.auth_metadata["issuer"],
        authorization_endpoint=pending.auth_metadata["authorization_endpoint"],
        token_endpoint=pending.auth_metadata["token_endpoint"],
        registration_endpoint=pending.auth_metadata.get("registration_endpoint"),
        scopes_supported=pending.auth_metadata.get("scopes_supported", []),
        code_challenge_methods_supported=pending.auth_metadata.get("code_challenge_methods_supported", []),
    )

    class _VerifierOnly:
        def __init__(self, verifier: str) -> None:
            self.verifier = verifier

    try:
        token_data = await exchange_code(
            metadata=auth_meta,
            code=code or "",
            redirect_uri=OAUTH_REDIRECT_URI,
            client_id=pending.client_id,
            pkce=_VerifierOnly(pending.pkce_verifier),  # type: ignore[arg-type]
            client_secret=pending.client_secret,
        )
    except Exception as exc:
        logging.warning("OAuth code exchange failed for user %s tool %s: %s", pending.user_id, pending.tool_name, exc)
        return _frontend_redirect(pending.site, f"oauth_error={exc}")

    token_blob = {
        "client_id": pending.client_id,
        "client_secret": pending.client_secret,
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": int(time.time()) + int(token_data.get("expires_in", 3600)),
        "token_type": token_data.get("token_type", "Bearer"),
        "scope": token_data.get("scope", ""),
        "server_url": pending.server_url,
        "auth_server_metadata": pending.auth_metadata,
    }

    try:
        _save_oauth_token(pending.user_id, pending.tool_name, token_blob)
    except Exception as exc:
        logging.error("Failed to store OAuth token for user %s tool %s: %s", pending.user_id, pending.tool_name, exc)
        return _frontend_redirect(pending.site, f"oauth_error=Failed+to+store+token")

    return _frontend_redirect(pending.site, f"oauth_success={pending.tool_name}")


# ── AWS SSO ────────────────────────────────────────────────────────────────────


async def _initiate_aws_sso(user_id: str, site: str) -> dict:
    """Start the AWS SSO device authorization flow.

    Uses sso-oidc:RegisterClient (dynamic) + sso-oidc:StartDeviceAuthorization,
    matching the flow used by 'aws sso login'. Returns a verification URL for the
    frontend to open in a new tab, plus a session key to poll for completion.
    """
    config = _get_aws_sso_client_config(site)
    if not config:
        raise HTTPException(
            status_code=400,
            detail="AWS SSO is not configured. Use GET/PUT /aws-sso/setup to set your SSO start URL and region.",
        )
    sso_start_url: str = config.get("sso_start_url", "").strip().rstrip("/")
    sso_region: str = config.get("sso_region", "us-east-1").strip()
    aws_region: str = config.get("aws_region", sso_region).strip()
    if not sso_start_url:
        raise HTTPException(status_code=400, detail="sso_start_url is missing from AWS SSO config.")

    logging.info("Starting AWS SSO device auth: start_url=%r region=%r", sso_start_url, sso_region)

    oidc = boto3.client("sso-oidc", region_name=sso_region)

    try:
        reg = oidc.register_client(
            clientName="AgentScribe",
            clientType="public",
        )
    except Exception as exc:
        logging.error("RegisterClient failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AWS SSO client registration failed: {exc}") from exc

    client_id: str = reg["clientId"]
    client_secret: str = reg["clientSecret"]
    logging.info("RegisterClient OK: client_id=%r", client_id)

    try:
        auth = oidc.start_device_authorization(
            clientId=client_id,
            clientSecret=client_secret,
            startUrl=sso_start_url,
        )
    except Exception as exc:
        logging.error("StartDeviceAuthorization failed (start_url=%r region=%r): %s", sso_start_url, sso_region, exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"AWS SSO device authorization failed (start_url={sso_start_url!r}, region={sso_region!r}): {exc}",
        ) from exc

    _cleanup_aws_sso_state()
    state = secrets.token_urlsafe(32)
    _aws_sso_pending[state] = _AwsSsoPendingState(
        user_id=user_id,
        site=site,
        sso_region=sso_region,
        sso_start_url=sso_start_url,
        aws_region=aws_region,
        client_id=client_id,
        client_secret=client_secret,
        device_code=auth["deviceCode"],
        created_at=time.time(),
        expires_in=auth.get("expiresIn", 600),
        interval=auth.get("interval", 5),
    )

    return {
        "auth_url": auth["verificationUriComplete"],
        "user_code": auth.get("userCode", ""),
        "session": state,
        "polling_interval": auth.get("interval", 5),
        "expires_in": auth.get("expiresIn", 600),
    }


class _AwsSsoSetupRequest(BaseModel):
    sso_start_url: str
    sso_region: str
    aws_region: str = ""


class _AwsSsoSelectRoleRequest(BaseModel):
    account_id: str
    role_name: str


@app.get("/aws-sso/setup", tags=["oauth"], summary="Get AWS SSO configuration for a site")
async def aws_sso_get_setup(
    site: str = Query(...),
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return the current AWS SSO configuration (sso_start_url, sso_region) for the site."""
    user_id, user_site = ctx
    if user_site is None or user_site != site:
        raise HTTPException(status_code=403, detail="Access denied")
    return _get_aws_sso_client_config(site) or {}


@app.put("/aws-sso/setup", tags=["oauth"], summary="Save AWS SSO configuration for a site")
async def aws_sso_put_setup(
    body: _AwsSsoSetupRequest,
    site: str = Query(...),
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Write sso_start_url and sso_region to {site}/.aws-sso/aws-sso-client.json."""
    user_id, user_site = ctx
    if user_site is None or user_site != site:
        raise HTTPException(status_code=403, detail="Access denied")
    _save_aws_sso_client_config(site, {
        "sso_start_url": body.sso_start_url,
        "sso_region": body.sso_region,
        "aws_region": body.aws_region or body.sso_region,
    })
    return {"status": "ok"}


@app.get("/aws-sso/auth-status", tags=["oauth"], summary="Poll AWS SSO device authorization status")
async def aws_sso_auth_status(
    session: str = Query(...),
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Poll for completion of the AWS SSO device authorization flow.

    Returns:
      {"status": "pending"}   — user has not yet approved in the browser
      {"status": "authorized", "accounts": [...]}  — approved; accounts listed for role picker
      {"status": "expired"}   — the device code has expired; user must restart
      {"status": "error", "error": "..."}  — unexpected error
    """
    user_id, _ = ctx

    _cleanup_aws_sso_state()
    pending = _aws_sso_pending.get(session)
    if not pending:
        raise HTTPException(status_code=404, detail="Session not found or expired. Please re-authenticate.")
    if pending.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    oidc = _boto_session.client("sso-oidc", region_name=pending.sso_region)
    try:
        token_response = oidc.create_token(
            clientId=pending.client_id,
            clientSecret=pending.client_secret,
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=pending.device_code,
        )
    except oidc.exceptions.AuthorizationPendingException:
        return {"status": "pending"}
    except oidc.exceptions.SlowDownException:
        return {"status": "pending"}
    except oidc.exceptions.ExpiredTokenException:
        del _aws_sso_pending[session]
        return {"status": "expired"}
    except Exception as exc:
        del _aws_sso_pending[session]
        logging.warning("AWS SSO token exchange failed for user %s: %s", user_id, exc)
        return {"status": "error", "error": str(exc)}

    access_token: str = token_response["accessToken"]
    del _aws_sso_pending[session]

    # Persist token as pending (no account/role yet) in Secrets Manager.
    pending_blob = {
        "access_token": access_token,
        "refresh_token": token_response.get("refreshToken"),
        "expires_at": int(time.time()) + int(token_response.get("expiresIn", 3600)),
        "client_id": pending.client_id,
        "client_secret": pending.client_secret,
        "sso_region": pending.sso_region,
        "sso_start_url": pending.sso_start_url,
        "aws_region": pending.aws_region,
    }
    _save_oauth_token(user_id, "aws-sso-pending", pending_blob)

    # Fetch available accounts for the role picker.
    sso_client = _boto_session.client("sso", region_name=pending.sso_region)
    try:
        accounts_resp = sso_client.list_accounts(accessToken=access_token, maxResults=100)
        accounts = [
            {
                "account_id": a["accountId"],
                "account_name": a["accountName"],
                "email": a.get("emailAddress", ""),
            }
            for a in accounts_resp.get("accountList", [])
        ]
    except Exception as exc:
        logging.warning("Failed to list SSO accounts for user %s: %s", user_id, exc)
        accounts = []

    return {"status": "authorized", "accounts": accounts}


@app.get("/aws-sso/roles/{account_id}", tags=["oauth"], summary="List roles for an AWS SSO account")
async def aws_sso_list_roles(
    account_id: str,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return the permission set roles available in an AWS account for the pending SSO token."""
    user_id, _ = ctx
    token = _load_oauth_token(user_id, "aws-sso-pending")
    if not token:
        raise HTTPException(status_code=404, detail="No pending AWS SSO session. Please re-authenticate.")

    sso_region: str = token.get("sso_region", "us-east-1")
    sso_client = _boto_session.client("sso", region_name=sso_region)
    try:
        roles_resp = sso_client.list_account_roles(
            accessToken=token["access_token"],
            accountId=account_id,
            maxResults=100,
        )
        roles = [{"role_name": r["roleName"]} for r in roles_resp.get("roleList", [])]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list roles: {exc}") from exc

    return {"roles": roles}


@app.post("/aws-sso/select-role", tags=["oauth"], summary="Confirm AWS account and role selection")
async def aws_sso_select_role(
    body: _AwsSsoSelectRoleRequest,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Attach the chosen account_id and role_name to the pending token and promote it to active.

    Verifies the credentials work before storing, then cleans up the pending secret.
    """
    user_id, _ = ctx
    token = _load_oauth_token(user_id, "aws-sso-pending")
    if not token:
        raise HTTPException(status_code=404, detail="No pending AWS SSO session. Please re-authenticate.")

    sso_region: str = token.get("sso_region", "us-east-1")
    sso_client = _boto_session.client("sso", region_name=sso_region)

    # Verify the selection is valid before committing.
    try:
        sso_client.get_role_credentials(
            accountId=body.account_id,
            roleName=body.role_name,
            accessToken=token["access_token"],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not get credentials for {body.role_name} in account {body.account_id}: {exc}",
        ) from exc

    token["account_id"] = body.account_id
    token["role_name"] = body.role_name
    _save_oauth_token(user_id, "aws-sso", token)

    # Clean up the pending secret (best-effort).
    try:
        sm.delete_secret(
            SecretId=_oauth_secret_id(user_id, "aws-sso-pending"),
            ForceDeleteWithoutRecovery=True,
        )
    except Exception:
        pass

    return {"status": "ok", "account_id": body.account_id, "role_name": body.role_name}


@app.put("/secrets/{var_name}", tags=["secrets"], summary="Store or update a credential")
async def put_secret(
    var_name: str,
    body: SecretValue,
    user_id: str = Depends(_get_user_id),
) -> dict[str, str]:
    """Store or update a credential value in Secrets Manager for the current user."""
    if not _VAR_NAME_RE.match(var_name):
        raise HTTPException(status_code=400, detail="Invalid variable name")
    secret_id = _secret_id(user_id, var_name)
    try:
        sm.put_secret_value(SecretId=secret_id, SecretString=body.value)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=secret_id,
            SecretString=body.value,
            Description=f"AgentScribe credential: {var_name} for user {user_id}",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "stored"}


@app.post(
    "/chat",
    tags=["chat"],
    summary="Chat with the AI agent",
    description=(
        "Send a user message to the ChatAgent orchestrator. The agent may read/write "
        "page content, create agents, create pages, or enqueue async runner jobs. "
        "Requires site ownership. Returns the agent's reply and optionally updated content "
        "or a navigation target."
    ),
    response_model=ChatResponse,
)
async def chat(
    req: ChatRequest,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> Any:
    user_id, user_site = ctx
    _require_site_owner(req.site, user_site)
    if not _is_safe_path(req.file_path):
        raise HTTPException(status_code=400, detail="Invalid file_path")

    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant")
    ]

    try:
        reply, updated_content, navigate_to = chat_agent.run(
            message=req.message,
            current_content=req.current_content,
            history=history,
            site=req.site,
            file_path=req.file_path,
            user_id=user_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if updated_content is not None:
        content_key = f"{req.site}/{req.file_path}"
        if req.file_path == "content.md" or req.file_path.endswith("/content.md"):
            _enqueue_index_job(content_key, user_id)

    return ChatResponse(reply=reply, updated_content=updated_content, navigate_to=navigate_to)


@app.post(
    "/provision",
    tags=["site"],
    summary="Provision a new site",
    description=(
        "Create a new site in S3, insert the user→site mapping in Supabase, "
        "and provision IAM/K8s/Secrets Manager infrastructure. Each user may only "
        "provision one site. Returns the public URL of the new site."
    ),
)
async def provision(
    req: ProvisionRequest,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> ProvisionResponse:
    user_id, existing_site = ctx

    if existing_site is not None:
        raise HTTPException(status_code=409, detail="User already has a provisioned site")

    if not SITE_NAME_RE.match(req.site_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid site name: must be 3-50 lowercase alphanumeric characters or hyphens, not starting or ending with a hyphen",
        )

    if req.theme not in VALID_THEMES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid theme; must be one of: {', '.join(sorted(VALID_THEMES))}",
        )

    # Check S3 uniqueness
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{req.site_name}/", MaxKeys=1)
    if resp.get("KeyCount", 0) > 0:
        raise HTTPException(status_code=409, detail="Site name already taken")

    # Create initial S3 objects
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/content.md",
        Body=_DEFAULT_WELCOME_MD.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/config.json",
        Body=json.dumps({"theme": req.theme}).encode("utf-8"),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site_name}/settings.json",
        Body=json.dumps({"visibility": "private", "shared_with": []}).encode("utf-8"),
        ContentType="application/json",
    )

    # Copy pre-built theme bundle from _themes/{theme}/ → {site_name}/
    theme_prefix = f"_themes/{req.theme}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=theme_prefix):
        for obj in page.get("Contents", []):
            src_key = obj["Key"]
            dst_key = f"{req.site_name}/" + src_key[len(theme_prefix):]
            s3.copy_object(
                Bucket=S3_BUCKET,
                CopySource={"Bucket": S3_BUCKET, "Key": src_key},
                Key=dst_key,
            )

    # Insert into user_site table
    _supabase_insert_user_site(user_id, req.site_name, req.theme)

    # Provision IAM/K8s/SM infrastructure
    try:
        await _provision_user_infrastructure(user_id, req.site_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    site_url = (
        f"https://{CLOUDFRONT_DOMAIN}/{req.site_name}"
        if CLOUDFRONT_DOMAIN
        else f"/{req.site_name}"
    )
    return ProvisionResponse(site_url=site_url)


@app.get("/my-site", tags=["site"], summary="Get the current user's site name")
async def my_site(
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return the authenticated user's site name from their JWT app_metadata."""
    _, site_name = ctx
    return {"site_name": site_name}


@app.delete("/account", tags=["site"], summary="Delete account and all associated resources")
async def delete_account(
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict[str, str]:
    """Permanently delete the authenticated user's account and all associated resources."""
    user_id, site_name = ctx

    # 1. Delete search vectors then S3 prefix (vectors first — chunk keys must
    #    exist in S3 so we can enumerate their UUIDs as vector IDs).
    if site_name is not None:
        _delete_site_vectors(site_name)
        try:
            _delete_s3_prefix(site_name)
        except Exception as exc:
            logging.warning("S3 prefix delete warning for %s: %s", site_name, exc)

    # 2. Deprovision AWS infrastructure (best-effort)
    await _deprovision_user_infrastructure(user_id, site_name)

    # 3. Delete user_site row (best-effort)
    _supabase_delete_user_site(user_id)

    # 4. Delete Supabase auth user — hard stop on failure; must be last so the
    #    request JWT remains valid throughout all preceding steps.
    _supabase_delete_auth_user(user_id)

    return {"status": "deleted"}


@app.post("/webhooks/user-created", tags=["webhooks"], summary="Webhook: provision infrastructure for a new user")
async def user_created(request: Request, event: UserCreatedEvent) -> dict[str, str]:
    """Provision IAM role, K8s ServiceAccount, and Secrets Manager placeholder for a new user."""
    secret = request.headers.get("x-webhook-secret", "")
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    site_name = _get_site_for_user(event.user_id)
    if site_name is None:
        raise HTTPException(status_code=400, detail="No site found for user; provision a site first")

    try:
        await _provision_user_infrastructure(event.user_id, site_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"status": "ok"}


# ── MCP OAuth 2.0 Authorization Server ────────────────────────────────────────
#
# These endpoints turn AgentScribe into an OAuth AS for Claude Code so that
# users can authenticate with their Google/Supabase identity without knowing
# anything about an OAuth client.
#
# Flow:
#   1. Claude Code discovers the AS via GET /.well-known/oauth-authorization-server
#   2. Claude Code registers itself via POST /mcp/oauth/register (DCR, RFC 7591)
#   3. Claude Code sends the user to GET /mcp/oauth/authorize — we generate our
#      own PKCE pair, store Claude Code's PKCE challenge, and redirect the user
#      to Supabase's Google OAuth endpoint with *our* PKCE challenge embedded.
#   4. Supabase redirects back to GET /mcp/oauth/callback/{mcp_state}?code=...
#      We exchange the code for a Supabase JWT (server-side PKCE), store it as
#      a one-time code, and redirect Claude Code to its redirect_uri?code=...
#   5. Claude Code calls POST /mcp/oauth/token with the code + its PKCE verifier.
#      We verify S256(verifier) == challenge and return the Supabase JWT.
#
# Entirely separate from the existing /oauth/* skill auth flow.


@app.get(
    "/.well-known/oauth-authorization-server",
    tags=["mcp"],
    summary="OAuth 2.0 Authorization Server Metadata (RFC 8414)",
    include_in_schema=True,
)
async def mcp_oauth_server_metadata() -> dict:
    """Return RFC 8414 OAuth AS metadata so Claude Code can discover our endpoints."""
    base = _mcp_api_base()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@app.post(
    "/mcp/oauth/register",
    tags=["mcp"],
    summary="Dynamic Client Registration (RFC 7591)",
    status_code=201,
)
async def mcp_oauth_register(request: Request) -> dict:
    """Register a new OAuth client (Claude Code) and return a client_id.

    Accepts any redirect_uris — no client secret is issued (public client).
    """
    body = await request.json()
    redirect_uris: list[str] = body.get("redirect_uris", [])
    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")

    client_id = str(uuid.uuid4())
    _mcp_dcr_clients[client_id] = _McpDcrClient(
        client_id=client_id,
        redirect_uris=redirect_uris,
        created_at=time.time(),
    )
    return {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@app.get(
    "/mcp/oauth/authorize",
    tags=["mcp"],
    summary="OAuth 2.0 authorization endpoint — initiates Google login via Supabase",
)
async def mcp_oauth_authorize(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    state: str | None = Query(default=None),
    scope: str | None = Query(default=None),
) -> RedirectResponse:
    """Validate the authorization request and redirect the user to Supabase/Google."""
    _cleanup_mcp_state()

    if response_type != "code":
        raise HTTPException(status_code=400, detail="Only response_type=code is supported")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="Only S256 code_challenge_method is supported")

    # Validate redirect_uri: must be loopback (Claude Code) or registered via DCR.
    # We don't require prior DCR registration here — server reloads wipe in-memory
    # state and Claude Code would silently re-register anyway. PKCE (S256) provides
    # the actual security; client_id is just a correlation identifier.
    parsed_redir = urllib.parse.urlparse(redirect_uri)
    is_loopback = parsed_redir.hostname in ("localhost", "127.0.0.1", "::1")
    registered = _mcp_dcr_clients.get(client_id)
    if not is_loopback and (registered is None or redirect_uri not in registered.redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri must be a loopback address or pre-registered via DCR")

    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase is not configured on this server")

    # Generate AgentScribe→Supabase PKCE verifier
    supabase_verifier = secrets.token_urlsafe(48)
    supabase_challenge = _pkce_s256(supabase_verifier)

    # Internal state key embeds in the callback path so Supabase doesn't lose it
    internal_state = secrets.token_urlsafe(32)
    _mcp_auth_pending[internal_state] = _McpAuthPending(
        client_id=client_id,
        cc_code_challenge=code_challenge,
        cc_code_challenge_method=code_challenge_method,
        cc_redirect_uri=redirect_uri,
        cc_state=state,
        supabase_pkce_verifier=supabase_verifier,
        created_at=time.time(),
    )

    base = _mcp_api_base()
    callback_url = f"{base}/mcp/oauth/callback/{urllib.parse.quote(internal_state, safe='')}"
    supabase_auth_url = (
        f"{SUPABASE_URL}/auth/v1/authorize"
        f"?provider=google"
        f"&code_challenge={urllib.parse.quote(supabase_challenge, safe='')}"
        f"&code_challenge_method=S256"
        f"&redirect_to={urllib.parse.quote(callback_url, safe='')}"
    )
    return RedirectResponse(url=supabase_auth_url, status_code=302)


@app.get(
    "/mcp/oauth/callback/{mcp_state}",
    tags=["mcp"],
    summary="Supabase OAuth callback — exchanges Supabase code for JWT, issues code to client",
)
async def mcp_oauth_callback(
    mcp_state: str,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Receive the authorization code from Supabase and issue a one-time code to Claude Code."""
    _cleanup_mcp_state()

    pending = _mcp_auth_pending.get(mcp_state)
    if not pending:
        return Response(  # type: ignore[return-value]
            content="Invalid or expired OAuth state. Please restart the authentication flow.",
            status_code=400,
        )

    if error:
        del _mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": error, "error_description": error_description or error})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    if not code:
        del _mcp_auth_pending[mcp_state]
        return Response(content="Missing code parameter from Supabase.", status_code=400)  # type: ignore[return-value]

    # Exchange Supabase code for JWT using server-side PKCE
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                f"{SUPABASE_URL}/auth/v1/token",
                params={"grant_type": "pkce"},
                json={"auth_code": code, "code_verifier": pending.supabase_pkce_verifier},
                headers={"apikey": SUPABASE_SERVICE_ROLE_KEY},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
    except Exception as exc:
        logging.warning("MCP OAuth: Supabase code exchange failed: %s", exc)
        del _mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": "server_error", "error_description": "Token exchange failed"})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    supabase_jwt: str = token_data.get("access_token", "")
    supabase_refresh: str | None = token_data.get("refresh_token")

    if not supabase_jwt:
        del _mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": "server_error", "error_description": "No access token returned"})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    del _mcp_auth_pending[mcp_state]

    # Issue a one-time authorization code to Claude Code
    our_code = secrets.token_urlsafe(32)
    _mcp_codes[our_code] = _McpCode(
        client_id=pending.client_id,
        supabase_jwt=supabase_jwt,
        supabase_refresh_token=supabase_refresh,
        cc_code_challenge=pending.cc_code_challenge,
        cc_code_challenge_method=pending.cc_code_challenge_method,
        cc_redirect_uri=pending.cc_redirect_uri,
        cc_state=pending.cc_state,
        created_at=time.time(),
    )

    params: dict[str, str] = {"code": our_code}
    if pending.cc_state:
        params["state"] = pending.cc_state
    redirect_url = f"{pending.cc_redirect_uri}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@app.post(
    "/mcp/oauth/token",
    tags=["mcp"],
    summary="OAuth 2.0 token endpoint — exchange code or refresh token for Supabase JWT",
)
async def mcp_oauth_token(request: Request) -> dict:
    """Exchange an authorization code (+ PKCE verifier) or refresh token for a Supabase JWT.

    For authorization_code grant: validates the S256 PKCE challenge and returns the
    Supabase JWT as the access_token so the MCP server can validate it directly.

    For refresh_token grant: forwards the refresh token to Supabase and returns new tokens.
    """
    # Support both application/x-www-form-urlencoded and application/json
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type: str = body.get("grant_type", "")

    if grant_type == "authorization_code":
        code: str = body.get("code", "")
        code_verifier: str = body.get("code_verifier", "")

        stored = _mcp_codes.get(code)
        if not stored:
            raise HTTPException(status_code=400, detail="Invalid or expired authorization code")

        # Verify PKCE S256
        expected_challenge = _pkce_s256(code_verifier)
        if expected_challenge != stored.cc_code_challenge:
            raise HTTPException(status_code=400, detail="PKCE verification failed")

        del _mcp_codes[code]

        response: dict = {
            "access_token": stored.supabase_jwt,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if stored.supabase_refresh_token:
            response["refresh_token"] = stored.supabase_refresh_token
        return response

    elif grant_type == "refresh_token":
        refresh_token: str = body.get("refresh_token", "")
        if not refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token is required")
        if not SUPABASE_URL:
            raise HTTPException(status_code=503, detail="Supabase is not configured on this server")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{SUPABASE_URL}/auth/v1/token",
                    params={"grant_type": "refresh_token"},
                    json={"refresh_token": refresh_token},
                    headers={"apikey": SUPABASE_SERVICE_ROLE_KEY},
                )
                resp.raise_for_status()
                token_data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Token refresh failed: {exc}") from exc

        result: dict = {
            "access_token": token_data.get("access_token", ""),
            "token_type": "Bearer",
            "expires_in": token_data.get("expires_in", 3600),
        }
        if token_data.get("refresh_token"):
            result["refresh_token"] = token_data["refresh_token"]
        return result

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported grant_type: {grant_type}")
