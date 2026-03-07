"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import dataclasses
import datetime
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
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
from agents.base import DEFAULT_MODEL, agents_prefix, skills_prefix

import jwt as pyjwt
from jwt import PyJWKClient

app = FastAPI(title="AgentScribe API")

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
MODEL = os.environ.get("AGENTSCRIBE_MODEL", DEFAULT_MODEL)
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
    skill_name: str
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


# ── ChatAgent ─────────────────────────────────────────────────────────────────

chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    model_id=MODEL,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
    sm_client=sm,
)

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
            "Sid": "S3ReadSkillsPrefix",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": f"{s3_bucket_arn}/.skills/*",
        },
        {
            "Sid": "S3ListUserPrefix",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": s3_bucket_arn,
            "Condition": {
                "StringLike": {"s3:prefix": [f"{site_name}/*", ".skills/*"]}
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/content")
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
        content = _get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "view"
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


@app.put("/content")
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


@app.get("/agents")
async def list_agents(site: str = "default", page_path: str = "") -> dict:
    prefix = agents_prefix(site, page_path)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"agents": names}


@app.get("/pages")
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


@app.post("/pages")
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


@app.get("/skills")
async def list_skills() -> dict:
    prefix = skills_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"skills": names}


@app.get("/settings")
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


@app.put("/settings")
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


@app.post("/request-access")
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


def _oauth_secret_id(user_id: str, skill_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/oauth/{skill_name}"


def _is_remote_skill(skill_name: str) -> bool:
    """Return True if the skill's mcp.json uses remote HTTP transport."""
    key = f"{skills_prefix()}/{skill_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        config = json.loads(obj["Body"].read())
        return any("url" in srv for srv in config.get("mcpServers", {}).values())
    except Exception:
        return False


def _skill_required_vars(skill_name: str) -> list[str]:
    """Read a stdio skill's mcp.json from S3 and return required ${VAR} names."""
    key = f"{skills_prefix()}/{skill_name}/mcp.json"
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


def _load_skill_oauth_client(skill_name: str) -> dict | None:
    """Load pre-registered OAuth client config from S3 for a skill.

    Returns the parsed oauth_client.json dict, or None if the file does not exist.
    This file is present for skills whose MCP servers do not support DCR (e.g. GitHub).
    Format: {"client_id": "...", "scopes": ["repo", ...]}  (scopes is optional)
    The corresponding client_secret is stored in Secrets Manager at
    agentscribe/platform/oauth/{skill_name}.
    """
    key = f"{skills_prefix()}/{skill_name}/oauth_client.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _load_platform_client_secret(skill_name: str) -> str | None:
    """Load the platform-level OAuth client_secret from Secrets Manager.

    Secret path: agentscribe/platform/oauth/{skill_name}
    Expected format: {"client_secret": "..."}
    The backend IAM role must have secretsmanager:GetSecretValue on this path.
    Returns None if the secret does not exist or cannot be read.
    """
    secret_id = f"agentscribe/platform/oauth/{skill_name}"
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        data = json.loads(resp["SecretString"])
        return data.get("client_secret")
    except Exception:
        return None


def _load_oauth_token(user_id: str, skill_name: str) -> dict | None:
    """Load a stored OAuth token blob from Secrets Manager, or None if not found."""
    try:
        resp = sm.get_secret_value(SecretId=_oauth_secret_id(user_id, skill_name))
        return json.loads(resp["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        return None
    except Exception:
        return None


def _save_oauth_token(user_id: str, skill_name: str, token_blob: dict) -> None:
    """Create or update the OAuth token blob in Secrets Manager."""
    secret_id = _oauth_secret_id(user_id, skill_name)
    secret_string = json.dumps(token_blob)
    try:
        sm.put_secret_value(SecretId=secret_id, SecretString=secret_string)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=secret_id,
            SecretString=secret_string,
            Description=f"OAuth tokens for user {user_id} skill {skill_name}",
        )


@app.get("/secrets/status")
async def get_secrets_status(user_id: str = Depends(_get_user_id)) -> dict:
    """Return all skills with their credential status for this user.

    Remote OAuth skills return: {type: "oauth", authenticated, expires_at, scope}
    Stdio key-based skills return: {type: "key", vars, stored}
    """
    prefix = skills_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    skill_names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    skills: dict = {}
    for skill_name in skill_names:
        if _is_remote_skill(skill_name):
            token = _load_oauth_token(user_id, skill_name)
            if token:
                expires_at = token.get("expires_at")
                expires_str = (
                    datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
                    if expires_at
                    else None
                )
                skills[skill_name] = {
                    "type": "oauth",
                    "authenticated": True,
                    "expires_at": expires_str,
                    "scope": token.get("scope") or None,
                }
            else:
                skills[skill_name] = {
                    "type": "oauth",
                    "authenticated": False,
                    "expires_at": None,
                    "scope": None,
                }
        else:
            vars_needed = _skill_required_vars(skill_name)
            stored = {v: _secret_exists(user_id, v) for v in vars_needed}
            skills[skill_name] = {"type": "key", "vars": vars_needed, "stored": stored}

    return {"skills": skills}


@app.post("/oauth/initiate/{skill_name}")
async def oauth_initiate(
    skill_name: str,
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Begin the OAuth flow for a remote MCP skill.

    Performs OAuth discovery, then either:
    - Uses a pre-registered client from .skills/{skill_name}/oauth_client.json in S3
      (for servers like GitHub that don't support Dynamic Client Registration), or
    - Performs Dynamic Client Registration (RFC 7591) for servers that support it.

    Returns an authorization URL for the frontend to redirect the browser to.
    """
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")
    # Load the skill's mcp.json and extract the remote server URL
    key = f"{skills_prefix()}/{skill_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        mcp_config = json.loads(obj["Body"].read())
    except Exception:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found or has no mcp.json")

    server_url: str | None = None
    for server_cfg in mcp_config.get("mcpServers", {}).values():
        if "url" in server_cfg:
            server_url = server_cfg["url"]
            break
    if not server_url:
        raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' is not a remote MCP skill")

    _cleanup_oauth_state()

    # Check for a pre-registered OAuth client (skills whose servers don't support DCR).
    # oauth_client.json lives in S3 at .skills/{skill_name}/oauth_client.json and is
    # managed by the platform operator (not per-user).
    pre_registered = _load_skill_oauth_client(skill_name)

    # Discover OAuth metadata
    try:
        _prm, auth_meta = await discover(server_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OAuth discovery failed for {server_url}: {exc}") from exc
    if auth_meta is None:
        raise HTTPException(status_code=502, detail=f"No OAuth authorization server found for {server_url}")

    if pre_registered:
        # Use the platform-registered client — no DCR needed.
        client_id: str = pre_registered["client_id"]
        client_secret: str | None = _load_platform_client_secret(skill_name)
        # Skill-level scope override takes priority; fall back to server-advertised scopes.
        scopes: list[str] = pre_registered.get("scopes") or (
            list(auth_meta.scopes_supported) if auth_meta.scopes_supported else []
        )
    else:
        # Dynamic Client Registration (per-user client, servers that support RFC 7591).
        if not auth_meta.registration_endpoint:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Skill '{skill_name}' has no pre-registered OAuth client "
                    f"and the MCP server at {server_url} does not support "
                    f"Dynamic Client Registration. Upload an oauth_client.json "
                    f"to .skills/{skill_name}/ in S3 with the pre-registered client_id."
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
        skill_name=skill_name,
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


@app.get("/oauth/callback")
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
        logging.warning("OAuth code exchange failed for user %s skill %s: %s", pending.user_id, pending.skill_name, exc)
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
        _save_oauth_token(pending.user_id, pending.skill_name, token_blob)
    except Exception as exc:
        logging.error("Failed to store OAuth token for user %s skill %s: %s", pending.user_id, pending.skill_name, exc)
        return _frontend_redirect(pending.site, f"oauth_error=Failed+to+store+token")

    return _frontend_redirect(pending.site, f"oauth_success={pending.skill_name}")


@app.put("/secrets/{var_name}")
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


@app.post("/chat", response_model=ChatResponse)
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


@app.post("/provision")
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


@app.get("/my-site")
async def my_site(
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict:
    """Return the authenticated user's site name from their JWT app_metadata."""
    _, site_name = ctx
    return {"site_name": site_name}


@app.delete("/account")
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


@app.post("/webhooks/user-created")
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
