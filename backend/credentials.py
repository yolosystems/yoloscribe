"""Tool credential and OAuth token helpers (Secrets Manager + S3 tool ops)."""

import json
import logging
import re

from agents.base import tools_prefix
from config import S3_BUCKET, s3, secrets_store

# ── Constants ──────────────────────────────────────────────────────────────────

SM_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SM_SECRET_PREFIX = "yoloscribe"


# ── Secret ID helpers ──────────────────────────────────────────────────────────

def secret_id(user_id: str, var_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/{var_name}"


def oauth_secret_id(user_id: str, tool_name: str) -> str:
    return f"{_SM_SECRET_PREFIX}/{user_id}/oauth/{tool_name}"


# ── Tool introspection ─────────────────────────────────────────────────────────

def is_remote_tool(tool_name: str) -> bool:
    """Return True if the tool's mcp.json uses remote HTTP transport."""
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        config = json.loads(obj["Body"].read())
        return any("url" in srv for srv in config.get("mcpServers", {}).values())
    except Exception:
        return False


def tool_required_vars(tool_name: str) -> list[str]:
    """Read a stdio tool's mcp.json from S3 and return required ${VAR} names."""
    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        raw = obj["Body"].read().decode("utf-8")
        return list(dict.fromkeys(SM_VAR_RE.findall(raw)))
    except Exception:
        return []


def get_tool_auth_type(tool_name: str) -> str:
    """Return the auth type from a tool's mcp.json: 'oauth', 'aws-sso', 'none', or 'key'."""
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


def load_tool_oauth_client(tool_name: str) -> dict | None:
    """Load pre-registered OAuth client config from S3 for a tool.

    Returns the parsed oauth_client.json dict, or None if the file does not exist.
    """
    key = f"{tools_prefix()}/{tool_name}/oauth_client.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def load_platform_client_secret(tool_name: str) -> str | None:
    """Load the platform-level OAuth client_secret from Secrets Manager.

    Returns None in LOCAL_MODE — platform secrets are AWS-specific and not
    needed for local development.
    """
    sid = f"yoloscribe/platform/oauth/{tool_name}"
    raw = secrets_store.get(sid)
    if raw is None:
        return None
    try:
        return json.loads(raw).get("client_secret")
    except Exception:
        return None


# ── AWS SSO config ─────────────────────────────────────────────────────────────

def get_aws_sso_client_config(site: str) -> dict | None:
    """Read {site}/.aws-sso/aws-sso-client.json from S3, or None if absent."""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/.aws-sso/aws-sso-client.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None


def save_aws_sso_client_config(site: str, config: dict) -> None:
    """Write {site}/.aws-sso/aws-sso-client.json to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/.aws-sso/aws-sso-client.json",
        Body=json.dumps(config).encode("utf-8"),
        ContentType="application/json",
    )


# ── OAuth token storage ────────────────────────────────────────────────────────

def load_oauth_token(user_id: str, tool_name: str) -> dict | None:
    """Load a stored OAuth token blob, or None if not found."""
    raw = secrets_store.get(oauth_secret_id(user_id, tool_name))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_oauth_token(user_id: str, tool_name: str, token_blob: dict) -> None:
    """Create or update the OAuth token blob."""
    secrets_store.put(
        oauth_secret_id(user_id, tool_name),
        json.dumps(token_blob),
        description=f"OAuth tokens for user {user_id} tool {tool_name}",
    )


# ── Secrets Manager key-based credentials ─────────────────────────────────────

def secret_exists(user_id: str, var_name: str) -> bool:
    return secrets_store.exists(secret_id(user_id, var_name))


# ── User settings (enabled tools) ─────────────────────────────────────────────

def get_user_settings(site: str) -> dict:
    """Read {site}/.user/settings.json from S3; return {} if absent."""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/.user/settings.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception:
        return {}


def save_user_settings(site: str, settings: dict) -> None:
    """Write {site}/.user/settings.json to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/.user/settings.json",
        Body=json.dumps(settings).encode("utf-8"),
        ContentType="application/json",
    )
