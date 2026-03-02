"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

import boto3
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
)

# ── Configuration ─────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
MODEL = os.environ.get("AGENTSCRIBE_MODEL", DEFAULT_MODEL)
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "")

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
sm = _boto_session.client("secretsmanager", region_name=AWS_REGION)

chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    model_id=MODEL,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
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
    rf"|{PAGE_SEG}/content\.md"
    rf"|\.agents/{AGENT_NAME_SEG}/agent\.md"
    rf"|{PAGE_SEG}/\.agents/{AGENT_NAME_SEG}/agent\.md"
    r")$"
)

SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
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


# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _decode_jwt(credentials: HTTPAuthorizationCredentials | None) -> str:
    """Validate Supabase JWT and return user_id."""
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
        return payload["sub"]
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
    return _decode_jwt(credentials)


def _get_user_context(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> tuple[str, str | None]:
    """Extract user_id from JWT and look up site_name from user_site table."""
    user_id = _decode_jwt(credentials)
    return user_id, _get_site_for_user(user_id)


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
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="agentscribe-user-access",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "SecretsManagerReadUserSecrets",
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": f"{secret_arn_prefix}*",
                    },
                    {
                        "Sid": "S3ReadWriteUserPrefix",
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                        "Resource": f"{s3_bucket_arn}/{site_name}/*",
                    },
                    {
                        "Sid": "S3ListUserPrefix",
                        "Effect": "Allow",
                        "Action": "s3:ListBucket",
                        "Resource": s3_bucket_arn,
                        "Condition": {
                            "StringLike": {"s3:prefix": f"{site_name}/*"}
                        },
                    },
                ],
            }
        ),
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


class UserCreatedEvent(BaseModel):
    user_id: str


class SecretValue(BaseModel):
    value: str


class ProvisionRequest(BaseModel):
    site_name: str
    theme: str


class ProvisionResponse(BaseModel):
    site_url: str


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


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/content")
async def get_content(site: str = "default", path: str = "content.md") -> Response:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    content = _get_content(site, path)
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/content")
async def put_content(
    request: Request,
    site: str = "default",
    path: str = "content.md",
    ctx: tuple[str, str | None] = Depends(_get_user_context),
) -> dict[str, str]:
    user_id, user_site = ctx
    _require_site_owner(site, user_site)
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    body = await request.body()
    _put_content(site, path, body.decode("utf-8"))
    return {"status": "saved"}


@app.get("/agents")
async def list_agents(site: str = "default", page_path: str = "") -> dict:
    prefix = agents_prefix(site, page_path)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"agents": names}


@app.get("/skills")
async def list_skills() -> dict:
    prefix = skills_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"skills": names}


# ── Secrets (credentials) — no LLM involved ───────────────────────────────────

_SM_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SM_SECRET_PREFIX = "agentscribe"


def _secret_id(user_id: str, var_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/{var_name}"


def _skill_required_vars(skill_name: str) -> list[str]:
    """Read a skill's mcp.json from S3 and return required ${VAR} names."""
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


@app.get("/secrets/status")
async def get_secrets_status(user_id: str = Depends(_get_user_id)) -> dict:
    """Return all skills with their required vars and whether each is stored for this user."""
    prefix = skills_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    skill_names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    skills: dict = {}
    for skill_name in skill_names:
        vars_needed = _skill_required_vars(skill_name)
        stored = {v: _secret_exists(user_id, v) for v in vars_needed}
        skills[skill_name] = {"vars": vars_needed, "stored": stored}

    return {"skills": skills}


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
        reply, updated_content = chat_agent.run(
            message=req.message,
            current_content=req.current_content,
            history=history,
            site=req.site,
            file_path=req.file_path,
            user_id=user_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(reply=reply, updated_content=updated_content)


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

    # 1. Delete S3 prefix (best-effort)
    if site_name is not None:
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
