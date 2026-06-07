"""Agent runner — K8s Job entry point.

Reads an agent.md from S3, loads its MCP skills, and runs the agent against
the page's content.md, writing the result back to S3.

Skills use remote HTTP MCP servers, optionally authenticated via stored OAuth tokens.

Environment variables:
    BUCKET          S3 bucket name
    AGENT_MD_KEY    S3 key for the agent.md file
    CONTENT_KEY     S3 key for the content.md file
    AGENT_PROMPT    Task / instruction string passed to the agent
    USER_ID         User ID (used to resolve Secrets Manager secrets)
    AWS_REGION      AWS region
    ANTHROPIC_API_KEY  Anthropic API key
    AWS_PROFILE     (optional) named AWS profile for local development
    LOCAL_MCP_CONFIG_PATH  (optional) path to local-mcp-servers.json for LOCAL_MODE STDIO tools
                           (default: /app/local-mcp-servers.json)
    AGENT_RUNNER_MAX_PAGE_READS  max wiki_read calls per agent run (default: 10)
    SQS_QUEUE_URL  (optional) SQS dispatch queue; required to trigger on_notify agents
                   from confirm_page_change notifications
    SUPABASE_URL   (optional) Supabase project URL; enables token budget enforcement
    SUPABASE_SERVICE_ROLE_KEY  (optional) Supabase service role key
    TOKEN_BUDGET_DEFAULT_DAILY_LIMIT  (optional) default daily token limit (default: 500000)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

from .log_setup import configure_logging

log = logging.getLogger(__name__)

import boto3

from yoloscribe_io import (
    AgentDefinitionError,
    NotificationsMarkdownFile,
    S3StorageBackend,
    TokenData,
    ToolToken,
    WikiPageMarkdownFile,
    load_tool_config,
    parse_agent_md,
    parse_skill_md,
)
from .agents import (
    IngestAgent,
    NotificationAgent,
    NullSearchBackend,
    PageAgent,
    SearchBackend,
)

BUCKET = os.environ["BUCKET"]
AGENT_MD_KEY = os.environ["AGENT_MD_KEY"]
CONTENT_KEY = os.environ["CONTENT_KEY"]
AGENT_PROMPT = os.environ["AGENT_PROMPT"]
USER_ID = os.environ.get("USER_ID", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
SQS_ENDPOINT_URL = os.environ.get("SQS_ENDPOINT_URL", "")
LOCAL_MODE: bool = os.environ.get("LOCAL_MODE", "").lower() in ("1", "true", "yes")
LOCAL_MCP_CONFIG_PATH: str = os.environ.get("LOCAL_MCP_CONFIG_PATH", "/app/local-mcp-servers.json")
AGENT_RUNNER_MAX_PAGE_READS: int = int(os.environ.get("AGENT_RUNNER_MAX_PAGE_READS", "10"))
S3_VECTORS_BUCKET: str = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME: str = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TOKEN_BUDGET_DEFAULT_DAILY_LIMIT: int = int(os.environ.get("TOKEN_BUDGET_DEFAULT_DAILY_LIMIT", "500000"))

# ── Inline model registry ─────────────────────────────────────────────────────

_MODEL_REGISTRY: dict[str, tuple[str, str]] = {
    # key → (provider, model_id)
    "haiku":          ("anthropic", "claude-haiku-4-5-20251001"),
    "sonnet":         ("anthropic", "claude-sonnet-4-6"),
    "opus":           ("anthropic", "claude-opus-4-6"),
    "bedrock-haiku":  ("bedrock",   "anthropic.claude-haiku-4-5-20251001-v1:0"),
    "bedrock-sonnet": ("bedrock",   "anthropic.claude-sonnet-4-6-20250514-v1:0"),
    "bedrock-opus":   ("bedrock",   "anthropic.claude-opus-4-6-20250514-v1:0"),
}
_DEFAULT_MODEL_KEY = "sonnet"


def _resolve_model_key(*env_vars: str) -> str:
    for var in env_vars:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return _DEFAULT_MODEL_KEY


def _build_model(model_key: str):
    entry = _MODEL_REGISTRY.get(model_key)
    if entry is None:
        # Treat unrecognised keys as direct Bedrock model IDs or inference profile ARNs.
        from strands.models.bedrock import BedrockModel
        model_id = model_key if model_key else _MODEL_REGISTRY[_DEFAULT_MODEL_KEY][1]
        return BedrockModel(model_id=model_id, max_tokens=16384)
    provider, model_id = entry
    if provider == "anthropic":
        from strands.models.anthropic import AnthropicModel
        return AnthropicModel(
            model_id=model_id,
            max_tokens=16384,
            client_args={"max_retries": 0},
        )
    else:
        from strands.models.bedrock import BedrockModel
        return BedrockModel(model_id=model_id, max_tokens=16384)

# ── Inline token budget client ────────────────────────────────────────────────


class _SupabaseBudget:
    """Lightweight Supabase token budget client for the agent runner."""

    def __init__(self, url: str, key: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _today() -> str:
        import datetime
        return datetime.date.today().isoformat()

    def get_limit(self, user_id: str) -> int:
        import urllib.parse
        import urllib.request
        req = urllib.request.Request(
            f"{self._url}/rest/v1/token_budgets?select=daily_limit"
            f"&user_id=eq.{urllib.parse.quote(user_id)}",
            headers={**self._headers, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                rows = json.loads(resp.read())
                if rows:
                    return int(rows[0]["daily_limit"])
        except Exception as exc:
            log.warning("Failed to fetch token budget limit for %s: %s", user_id, exc)
        return TOKEN_BUDGET_DEFAULT_DAILY_LIMIT

    def get_used(self, user_id: str) -> int:
        import urllib.parse
        import urllib.request
        today = self._today()
        req = urllib.request.Request(
            f"{self._url}/rest/v1/token_usage?select=total_tokens"
            f"&user_id=eq.{urllib.parse.quote(user_id)}&usage_date=eq.{today}",
            headers={**self._headers, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                rows = json.loads(resp.read())
                if rows:
                    return int(rows[0]["total_tokens"])
        except Exception as exc:
            log.warning("Failed to fetch token usage for %s: %s", user_id, exc)
        return 0

    def record_usage(self, user_id: str, tokens: int) -> None:
        import urllib.request
        if tokens <= 0:
            return
        today = self._today()
        body = json.dumps({"p_user_id": user_id, "p_date": today, "p_tokens": tokens}).encode()
        req = urllib.request.Request(
            f"{self._url}/rest/v1/rpc/increment_token_usage",
            data=body,
            headers={**self._headers, "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            log.warning("Failed to record token usage for %s: %s", user_id, exc)


_session = boto3.Session(profile_name=AWS_PROFILE or None)


def _s3_client():
    kwargs = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
        # Use dedicated MINIO_* credentials so that AWS_ACCESS_KEY_ID /
        # AWS_SECRET_ACCESS_KEY are free for Bedrock in LOCAL_MODE.
        minio_key = os.environ.get("MINIO_ACCESS_KEY_ID")
        minio_secret = os.environ.get("MINIO_SECRET_ACCESS_KEY")
        if minio_key and minio_secret:
            kwargs["aws_access_key_id"] = minio_key
            kwargs["aws_secret_access_key"] = minio_secret
    return _session.client("s3", **kwargs)


def _make_secrets_store(s3):
    from yolo_secrets import make_secrets_store
    sm = None if LOCAL_MODE else _session.client("secretsmanager", region_name=AWS_REGION)
    return make_secrets_store(local_mode=LOCAL_MODE, s3_client=s3, bucket=BUCKET, sm_client=sm)


def _enqueue_index_job(content_key: str) -> None:
    """Send an indexing job to the SQS indexing queue (best-effort; never raises)."""
    if not SQS_INDEXING_QUEUE_URL:
        return
    try:
        sqs_kwargs = {"region_name": AWS_REGION}
        if SQS_ENDPOINT_URL:
            sqs_kwargs["endpoint_url"] = SQS_ENDPOINT_URL
        sqs = _session.client("sqs", **sqs_kwargs)
        sqs.send_message(
            QueueUrl=SQS_INDEXING_QUEUE_URL,
            MessageBody=json.dumps({"bucket": BUCKET, "content_key": content_key, "user_id": USER_ID}),
        )
        log.info("Enqueued indexing job for %s", content_key)
    except Exception:
        log.warning("Failed to enqueue indexing job for %s", content_key, exc_info=True)


# ── OAuth token management ─────────────────────────────────────────────────────


class OAuthTokenError(Exception):
    """Raised when an OAuth token cannot be loaded or refreshed for a tool."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"OAuth error for tool '{tool_name}': {reason}")
        self.tool_name = tool_name
        self.reason = reason


def _load_and_refresh_oauth_token(tool_name: str, user_id: str, store) -> dict:
    """Load the OAuth token for a tool from the secrets store.

    If the token is within 5 minutes of expiry and a refresh token is available,
    refreshes proactively and writes the updated token back.

    Raises OAuthTokenError if the token is missing or cannot be refreshed.
    """
    from mcp_oauth import OAuthError
    from mcp_oauth import refresh_access_token as _refresh
    from mcp_oauth.discovery import AuthorizationServerMetadata

    tool_token = ToolToken(user_id, tool_name, store)
    data = tool_token.load()
    if data is None:
        raise OAuthTokenError(
            tool_name,
            "No OAuth token found. Please open the Tools panel and authenticate this tool.",
        )
    token_data = data.to_dict()

    # Proactive refresh: refresh if the token expires within 5 minutes
    expires_at = token_data.get("expires_at", 0)
    if time.time() > expires_at - 300:
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise OAuthTokenError(
                tool_name,
                "Token has expired and no refresh token is available. Please re-authenticate in the Credentials panel.",
            )

        auth_meta_dict = token_data.get("auth_server_metadata", {})
        auth_meta = AuthorizationServerMetadata(
            issuer=auth_meta_dict.get("issuer", ""),
            authorization_endpoint=auth_meta_dict.get("authorization_endpoint", ""),
            token_endpoint=auth_meta_dict.get("token_endpoint", ""),
            registration_endpoint=auth_meta_dict.get("registration_endpoint"),
            scopes_supported=auth_meta_dict.get("scopes_supported", []),
            code_challenge_methods_supported=auth_meta_dict.get("code_challenge_methods_supported", []),
        )

        try:
            new_tokens = asyncio.run(
                _refresh(
                    metadata=auth_meta,
                    refresh_token=refresh_token,
                    client_id=token_data.get("client_id", ""),
                    client_secret=token_data.get("client_secret"),
                )
            )
        except OAuthError as exc:
            raise OAuthTokenError(tool_name, f"Token refresh failed: {exc}")

        token_data["access_token"] = new_tokens.get("access_token", token_data["access_token"])
        if "refresh_token" in new_tokens:
            token_data["refresh_token"] = new_tokens["refresh_token"]
        token_data["expires_at"] = int(time.time()) + int(new_tokens.get("expires_in", 3600))
        if "scope" in new_tokens:
            token_data["scope"] = new_tokens["scope"]

        try:
            tool_token.save(TokenData.from_dict(token_data))
            log.info("Refreshed and persisted OAuth token for tool '%s'", tool_name)
        except Exception as exc:
            log.warning("Failed to persist refreshed token for tool '%s': %s", tool_name, exc)

    return token_data


def _get_aws_sso_credential_headers(user_id: str, store) -> dict[str, str]:
    """Exchange the stored AWS SSO access token for temporary IAM credentials.

    Reads the aws-sso secret from the secrets store, calls sso:GetRoleCredentials,
    and returns credential headers to inject into internal MCP server requests.

    Raises OAuthTokenError if the token is missing, expired, or the exchange fails.
    """
    secret_key = f"yoloscribe/{user_id}/oauth/aws-sso"
    raw = store.get(secret_key)
    if raw is None:
        raise OAuthTokenError(
            "aws-sso",
            "No AWS SSO token found. Please open the Tools panel and sign in with AWS SSO.",
        )
    try:
        token_data: dict = json.loads(raw)
    except Exception as exc:
        raise OAuthTokenError("aws-sso", f"Failed to read AWS SSO token: {exc}")

    access_token: str = token_data.get("access_token", "")
    account_id: str = token_data.get("account_id", "")
    role_name: str = token_data.get("role_name", "")
    sso_region: str = token_data.get("sso_region", "us-east-1")
    aws_region: str = token_data.get("aws_region", sso_region)

    if not access_token or not account_id or not role_name:
        raise OAuthTokenError(
            "aws-sso",
            "AWS SSO token is incomplete (missing account_id or role_name). Please re-authenticate.",
        )

    # Check token expiry
    expires_at = token_data.get("expires_at", 0)
    if time.time() > expires_at - 60:
        raise OAuthTokenError(
            "aws-sso",
            "AWS SSO token has expired. Please open the Tools panel and sign in with AWS SSO again.",
        )

    sso = _session.client("sso", region_name=sso_region)
    try:
        creds_resp = sso.get_role_credentials(
            accountId=account_id,
            roleName=role_name,
            accessToken=access_token,
        )
    except Exception as exc:
        raise OAuthTokenError("aws-sso", f"Failed to get role credentials: {exc}")

    role_creds = creds_resp["roleCredentials"]
    return {
        "X-Aws-Access-Key-Id": role_creds["accessKeyId"],
        "X-Aws-Secret-Access-Key": role_creds["secretAccessKey"],
        "X-Aws-Session-Token": role_creds.get("sessionToken", ""),
        "X-Aws-Region": aws_region,
    }


# ── MCP client construction ───────────────────────────────────────────────────


def _collect_tool_names(skill_names: list[str], site: str, storage: S3StorageBackend) -> list[str]:
    """Resolve skill names → tool names via SKILL.md frontmatter."""
    log.info("Resolving tool names from skills: %s", skill_names)
    tool_names: list[str] = []
    seen: set[str] = set()
    for skill_name in skill_names:
        skill_key = f"{site}/.skills/{skill_name}/SKILL.md"
        try:
            skill_text = storage.read(skill_key)
            if skill_text is None:
                log.warning("SKILL.md not found for skill '%s' (key=%s)", skill_name, skill_key)
                continue
            names = parse_skill_md(skill_text).tools
        except Exception as exc:
            log.warning("Failed to read SKILL.md for skill '%s' (key=%s): %s", skill_name, skill_key, exc)
            continue
        if not names:
            log.warning("Skill '%s' has no tools defined — skipping", skill_name)
            continue
        log.info("Skill '%s' declares tools: %s", skill_name, names)
        for name in names:
            if name not in seen:
                seen.add(name)
                tool_names.append(name)
    log.info("Resolved tool names: %s", tool_names)
    return tool_names


def _load_local_mcp_config() -> dict[str, dict]:
    """Load local STDIO MCP server configs from LOCAL_MCP_CONFIG_PATH.

    Returns a dict mapping server_name → server_config, or empty dict if the
    file is absent or unparseable.
    """
    if not os.path.exists(LOCAL_MCP_CONFIG_PATH):
        return {}
    try:
        with open(LOCAL_MCP_CONFIG_PATH) as f:
            data = json.load(f)
        servers = data.get("mcpServers", {})
        log.info("Loaded %d local MCP server(s) from %s", len(servers), LOCAL_MCP_CONFIG_PATH)
        return servers
    except Exception as exc:
        log.warning("Failed to load local MCP config from %s: %s", LOCAL_MCP_CONFIG_PATH, exc)
        return {}


def _build_local_mcp_clients(skill_names: list[str], site: str, storage: S3StorageBackend) -> tuple[list, list]:
    """Build STDIO MCPClient instances from local-mcp-servers.json (LOCAL_MODE only).

    Only servers whose name appears in the tool names required by the agent's skills
    are loaded — this prevents the LLM from seeing and using unintended tools.
    Tool names are resolved from each skill's SKILL.md frontmatter (the same
    source used in production), then matched against local-mcp-servers.json by name.
    Each entry must have a "command" field; "args" and "env" are optional.
    "env" is merged on top of the current process environment so PATH etc. are inherited.
    """
    try:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        from strands.tools.mcp import MCPClient
    except ImportError:
        log.warning("MCP stdio client not available; no local MCP tools will be loaded")
        return [], []

    local_servers = _load_local_mcp_config()
    if not local_servers:
        return [], []

    required_tool_names = set(_collect_tool_names(skill_names, site, storage))
    if not required_tool_names:
        log.warning("No tool names resolved from skills %s — no local MCP clients will be loaded", skill_names)
        return [], []

    clients: list = []

    for server_name, server_cfg in local_servers.items():
        if server_name not in required_tool_names:
            log.debug("Skipping local MCP server '%s' — not required by agent skills", server_name)
            continue

        command = server_cfg.get("command")
        if not command:
            log.warning("Local MCP server '%s' has no 'command' — skipping", server_name)
            continue

        args = server_cfg.get("args", [])
        env = {**os.environ, **server_cfg.get("env", {})}

        log.info("Building local STDIO MCP client for '%s' (command=%s)", server_name, command)
        params = StdioServerParameters(command=command, args=args, env=env)
        clients.append(
            MCPClient(
                lambda p=params: stdio_client(p),
                prefix=server_name,
            )
        )

    return clients, []


def _build_remote_mcp_clients(skill_names: list[str], site: str, storage: S3StorageBackend, store) -> tuple[list, list[OAuthTokenError]]:
    """Build HTTP MCPClient instances for all skills used by an agent.

    Resolution chain: agent.md → skill names → SKILL.md (tools list) → .tools/{name}/mcp.json

    Returns (clients, oauth_errors). OAuth errors are collected rather than
    raised so all missing tokens can be reported together.

    Remote tools with "auth": "oauth": connects via streamable HTTP with a Bearer token.
    Remote tools without "auth" (or "auth": "none"): connects via streamable HTTP unauthenticated.
    """
    try:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient
    except ImportError:
        log.warning("MCP package not available; no MCP tools will be loaded")
        return [], []

    clients: list = []
    errors: list[OAuthTokenError] = []
    tool_names = _collect_tool_names(skill_names, site, storage)

    for tool_name in tool_names:
        tool_cfg = load_tool_config(tool_name, storage)
        if tool_cfg is None:
            log.warning("No mcp.json found for tool '%s'", tool_name)
            continue

        for server_name, server_cfg in tool_cfg.raw_mcp.get("mcpServers", {}).items():
            if "command" in server_cfg:
                # Stdio tool bundled in the agent-runner container
                command = server_cfg["command"]
                args = server_cfg.get("args", [])
                env = {**os.environ, **server_cfg.get("env", {})}
                webhooks_set = bool(env.get("YOLOSCRIBE_WEBHOOKS", "[]").strip("[] \t"))
                log.info(
                    "Building stdio MCP client for tool '%s' server '%s' "
                    "(command=%s, YOLOSCRIBE_WEBHOOKS set=%s)",
                    tool_name, server_name, command, webhooks_set,
                )
                params = StdioServerParameters(command=command, args=args, env=env)
                clients.append(
                    MCPClient(
                        lambda p=params: stdio_client(p),
                        prefix=tool_name,
                    )
                )
                continue

            if "url" not in server_cfg:
                log.warning("Skipping MCP server '%s' in tool '%s': no url or command", server_name, tool_name)
                continue

            auth = server_cfg.get("auth", "none")
            headers: dict[str, str] = {}
            if auth == "oauth":
                try:
                    token_data = _load_and_refresh_oauth_token(tool_name, USER_ID, store)
                except OAuthTokenError as exc:
                    errors.append(exc)
                    continue
                headers["Authorization"] = f"Bearer {token_data['access_token']}"
            elif auth == "aws-sso":
                try:
                    headers = _get_aws_sso_credential_headers(USER_ID, store)
                except OAuthTokenError as exc:
                    errors.append(exc)
                    continue

            server_url = server_cfg["url"]
            log.info(
                "Building remote MCP client for tool '%s' server '%s' (auth=%s)",
                tool_name, server_name, auth,
            )
            clients.append(
                MCPClient(
                    lambda u=server_url, h=headers: streamablehttp_client(u, headers=h),
                    prefix=tool_name,
                )
            )

    return clients, errors


def _build_mcp_clients(skill_names: list[str], site: str, storage: S3StorageBackend, store) -> tuple[list, list[OAuthTokenError]]:
    """Build Strands MCPClient instances for all skills used by an agent.

    In LOCAL_MODE: reads local-mcp-servers.json and builds STDIO clients — no S3 tool
    lookup, no OAuth. In production: reads .tools/{name}/mcp.json from S3 and builds
    HTTP clients with OAuth/SSO auth as configured.
    """
    log.info("Building MCP clients for skills %s (LOCAL_MODE=%s)", skill_names, LOCAL_MODE)
    if LOCAL_MODE:
        return _build_local_mcp_clients(skill_names, site, storage)
    return _build_remote_mcp_clients(skill_names, site, storage, store)


# ── Tool name sanitisation ────────────────────────────────────────────────────

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _sanitize_tool_names(tools: list) -> None:
    """Sanitise MCP tool names in-place so they satisfy Claude's API constraint.

    Claude requires tool names to match ``^[a-zA-Z0-9_-]{1,128}``.  Some MCP
    servers (e.g. Google Workspace) emit names with dots, colons, or slashes.
    We replace invalid characters with ``_`` directly on ``_agent_tool_name``
    — the attribute Strands sends to Claude.  The original ``mcp_tool.name``
    is preserved, so MCP call routing is unaffected (Strands always uses the
    original name when calling back to the server).
    """
    for t in tools:
        name = getattr(t, "_agent_tool_name", None)
        if name and not _TOOL_NAME_RE.match(name):
            safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]
            log.info("Sanitizing tool name '%s' → '%s'", name, safe)
            t._agent_tool_name = safe


# ── Main ──────────────────────────────────────────────────────────────────────


class _ReadLimitedTool:
    """Proxy that caps wiki_read calls to AGENT_RUNNER_MAX_PAGE_READS per run."""

    def __init__(self, wrapped, counter: list[int], max_reads: int) -> None:
        self._wrapped = wrapped
        self._counter = counter
        self._max_reads = max_reads

    def __call__(self, **kwargs):
        if self._counter[0] >= self._max_reads:
            return (
                f"Error: Page read limit of {self._max_reads} reached. "
                f"This agent is not permitted to read more pages in a single run. "
                f"Complete your task based on what you have already read."
            )
        self._counter[0] += 1
        return self._wrapped(**kwargs)

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


def _apply_read_limit(tools: list, max_reads: int) -> list:
    """Wrap any wiki_read tools to enforce the per-run page read limit."""
    if max_reads <= 0:
        return tools
    counter: list[int] = [0]
    result = []
    for t in tools:
        name = getattr(t, "_agent_tool_name", None) or getattr(t, "__name__", "")
        if name == "wiki_read":
            result.append(_ReadLimitedTool(t, counter, max_reads))
        else:
            result.append(t)
    return result


def _make_search_backend(s3) -> SearchBackend:
    """Build the search backend from environment configuration.

    Returns NullSearchBackend when S3_VECTORS_BUCKET is not set so agents that
    don't need search still work in environments where search isn't configured.
    """
    if not S3_VECTORS_BUCKET:
        return NullSearchBackend()
    from .agents.search import BedrockS3VectorsSearchBackend
    try:
        bedrock = _session.client("bedrock-runtime", region_name=AWS_REGION)
        s3vectors = _session.client("s3vectors", region_name=AWS_REGION)
        return BedrockS3VectorsSearchBackend(
            bedrock_client=bedrock,
            s3vectors_client=s3vectors,
            s3_client=s3,
            bucket=BUCKET,
            vectors_bucket=S3_VECTORS_BUCKET,
            index_name=S3_VECTORS_INDEX_NAME,
        )
    except Exception as exc:
        log.warning("Could not build search backend, falling back to null: %s", exc)
        return NullSearchBackend()


def _make_agent(
    agent_def,
    site: str,
    page_path: str,
    wiki: WikiPageMarkdownFile,
    storage: S3StorageBackend,
    mcp_tools: list,
    model,
    search: SearchBackend,
    user_id: str,
    notify_fn,
    content_key: str = "",
):
    """Select and instantiate the appropriate agent subclass for this run."""
    # trigger=on_notify and page_path=.user/ingest are strong invariants that
    # win regardless of the type: field (handles agents predating the field).
    if agent_def.trigger == "on_notify":
        agent_type = "notification"
    elif page_path == ".user/ingest":
        agent_type = "ingest"
    else:
        agent_type = getattr(agent_def, "type", "") or "page"

    if agent_type == "notification":
        return NotificationAgent(
            agent_def=agent_def,
            site=site,
            page_path=page_path,
            storage=storage,
            mcp_tools=mcp_tools,
            model=model,
            user_id=user_id,
            notify_fn=notify_fn,
            search=search,
            max_page_reads=AGENT_RUNNER_MAX_PAGE_READS,
        )

    if agent_type == "ingest":
        return IngestAgent(
            agent_def=agent_def,
            site=site,
            page_path=page_path,
            storage=storage,
            mcp_tools=mcp_tools,
            model=model,
            user_id=user_id,
            notify_fn=notify_fn,
            search=search,
            max_page_reads=AGENT_RUNNER_MAX_PAGE_READS,
        )

    return PageAgent(
        agent_def=agent_def,
        site=site,
        page_path=page_path,
        wiki=wiki,
        storage=storage,
        mcp_tools=mcp_tools,
        model=model,
        user_id=user_id,
        notify_fn=notify_fn,
        search=search,
        max_page_reads=AGENT_RUNNER_MAX_PAGE_READS,
        content_key=content_key,
    )


def _write_run_log(
    storage: S3StorageBackend,
    run_log_key: str,
    agent_name: str,
    status: str,
    trigger: str,
    duration_s: float,
    detail: str = "",
) -> None:
    """Prepend a run log entry to the agent's run_log.md (best-effort; never raises)."""
    import datetime
    now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"## {agent_name} — {now}",
        "",
        f"**Status:** {status}  ",
        f"**Trigger:** {trigger}  ",
        f"**Duration:** {duration_s:.1f}s",
    ]
    if detail:
        lines += ["", detail]
    lines += ["", "---", ""]
    entry = "\n".join(lines) + "\n"
    try:
        existing = storage.read(run_log_key) or ""
        storage.write(run_log_key, entry + existing)
    except Exception as exc:
        log.warning("Failed to write run_log %s: %s", run_log_key, exc)


def main() -> None:
    configure_logging()
    log.info("Agent runner starting: bucket=%s agent_md=%s user=%s", BUCKET, AGENT_MD_KEY, USER_ID)

    # Expose the package directory so mcp.json files can reference bundled
    # helper scripts via ${AGENT_RUNNER_HOME} (substituted by _resolve_env_vars).
    os.environ.setdefault(
        "AGENT_RUNNER_HOME", os.path.dirname(os.path.abspath(__file__))
    )

    s3 = _s3_client()
    storage = S3StorageBackend(BUCKET, s3)
    store = _make_secrets_store(s3)

    _budget: _SupabaseBudget | None = (
        _SupabaseBudget(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        if not LOCAL_MODE and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
        else None
    )
    tokens_used = 0

    _run_start = time.monotonic()
    _site = AGENT_MD_KEY.split("/")[0]
    _run_log_key = AGENT_MD_KEY.rsplit("/", 1)[0] + "/run_log.md"

    # Derive the wiki page path from CONTENT_KEY (strips "{site}/" prefix and "/content.md" suffix).
    _content_rel = CONTENT_KEY[len(_site) + 1:]
    _page_path = "" if _content_rel == "content.md" else _content_rel[: -len("/content.md")]
    wiki = WikiPageMarkdownFile(site=_site, page_path=_page_path, storage=storage)

    # Build a lightweight SQS enqueue function for on_notify dispatch.
    # NotificationsMarkdownFile handles dispatch internally when enqueue is provided.
    def _make_enqueue_fn():
        if not SQS_QUEUE_URL:
            return None
        sqs_kwargs: dict = {"region_name": AWS_REGION}
        if SQS_ENDPOINT_URL:
            sqs_kwargs["endpoint_url"] = SQS_ENDPOINT_URL
        try:
            sqs = _session.client("sqs", **sqs_kwargs)
        except Exception as exc:
            log.warning("Could not create SQS client for on_notify dispatch: %s", exc)
            return None

        def _enqueue(agent_md_key: str, notifications_key: str, prompt: str, user_id: str) -> None:
            try:
                sqs.send_message(
                    QueueUrl=SQS_QUEUE_URL,
                    MessageBody=json.dumps({
                        "bucket": BUCKET,
                        "agent_md_key": agent_md_key,
                        "content_key": notifications_key,
                        "prompt": prompt,
                        "user_id": user_id,
                    }),
                )
                log.info("Enqueued on_notify agent %s for site %s", agent_md_key, _site)
            except Exception as exc:
                log.warning("Failed to enqueue on_notify agent %s: %s", agent_md_key, exc)

        return _enqueue

    _notif = NotificationsMarkdownFile(_site, storage, enqueue=_make_enqueue_fn())

    def _notify(event_type: str, payload: dict, user_id: str = "") -> None:
        try:
            _notif.notify(event_type, payload, user_id=user_id)
        except Exception as exc:
            log.error("Failed to write notification for site %s: %s", _site, exc)

    # content and agent_def are declared here so the top-level except block
    # can use them for error reporting even if early steps never ran.
    content = ""
    agent_def = None

    try:
        # 1. Read and parse agent.md
        raw_agent_md = storage.read(AGENT_MD_KEY)
        if raw_agent_md is None:
            raise FileNotFoundError(f"agent.md not found: {AGENT_MD_KEY}")
        try:
            agent_def = parse_agent_md(raw_agent_md)
        except AgentDefinitionError as exc:
            log.error("Invalid agent.md at %s: %s", AGENT_MD_KEY, exc)
            _notify("agent_failure", {"agent": AGENT_MD_KEY, "reason": f"Invalid agent definition: {exc}"})
            return

        # 1b. Pre-flight token budget check
        if _budget is not None:
            _used = _budget.get_used(USER_ID)
            _limit = _budget.get_limit(USER_ID)
            if _used >= _limit:
                log.error(
                    "Token budget exhausted for user %s (%d / %d tokens used)",
                    USER_ID, _used, _limit,
                )
                _notify(
                    "agent_failure",
                    {
                        "agent": AGENT_MD_KEY,
                        "reason": (
                            f"Daily token budget exhausted "
                            f"({_used:,} / {_limit:,} tokens used). "
                            "Resets at UTC midnight."
                        ),
                    },
                    USER_ID,
                )
                return

        # 2. Build MCP clients once — not repeated on write-conflict retries
        site = AGENT_MD_KEY.split("/")[0]
        mcp_clients, oauth_errors = _build_mcp_clients(agent_def.skills, site, storage, store)
        if oauth_errors:
            log.error("Aborting: OAuth token error(s): %s", [str(e) for e in oauth_errors])
            _notify(
                "agent_failure",
                {
                    "agent": AGENT_MD_KEY,
                    "reason": "; ".join(str(e) for e in oauth_errors),
                },
            )
            return

        # 3. Open MCP clients, collect tools, build model and agent
        mcp_tools: list = []
        log.info("Starting %d MCP client(s)", len(mcp_clients))
        with contextlib.ExitStack() as stack:
            for i, client in enumerate(mcp_clients):
                client_repr = getattr(client, "_name", None) or f"client[{i}]"
                try:
                    stack.enter_context(client)
                    log.info("MCP client started: %s", client_repr)
                except Exception as exc:
                    log.warning("MCP client failed to start (%s): %s", client_repr, exc)
                    continue
                try:
                    client_tools = client.list_tools_sync()
                    _sanitize_tool_names(client_tools)
                    tool_names_loaded = [
                        getattr(t, "_agent_tool_name", None) or getattr(t, "__name__", "?")
                        for t in client_tools
                    ]
                    mcp_tools.extend(client_tools)
                    log.info(
                        "Loaded %d tool(s) from %s: %s",
                        len(client_tools), client_repr, tool_names_loaded,
                    )
                except Exception as exc:
                    log.warning("Failed to list tools from MCP client (%s): %s", client_repr, exc)

            # Apply read limit to any MCP wiki_read tools (backward-compat for
            # agents that use the yoloscribe skill directly).
            mcp_tools = _apply_read_limit(mcp_tools, AGENT_RUNNER_MAX_PAGE_READS)
            log.info("Page read limit: %d wiki_read calls per run", AGENT_RUNNER_MAX_PAGE_READS)

            model_key = agent_def.model or _resolve_model_key(
                "YOLOSCRIBE_RUNNER_MODEL", "YOLOSCRIBE_MODEL"
            )
            model = _build_model(model_key)
            log.info("Using model key '%s' for agent '%s'", model_key, agent_def.name)

            search = _make_search_backend(s3)

            # 4. Create the typed agent and run it.
            agent = _make_agent(
                agent_def=agent_def,
                site=_site,
                page_path=_page_path,
                wiki=wiki,
                storage=storage,
                mcp_tools=mcp_tools,
                model=model,
                search=search,
                user_id=USER_ID,
                notify_fn=_notify,
                content_key=CONTENT_KEY,
            )
            tokens_used = agent.run(AGENT_PROMPT)

    except Exception as exc:
        log.error("Agent execution failed: %s", exc, exc_info=True)
        _trigger = agent_def.trigger if agent_def is not None else "manual"
        _agent_name = agent_def.name if agent_def is not None else "unknown"
        _write_run_log(
            storage, _run_log_key, _agent_name, "failed",
            _trigger, time.monotonic() - _run_start,
            f"Error: {exc}",
        )
        _notify("agent_failure", {"agent": AGENT_MD_KEY, "reason": str(exc)})
        return

    if agent_def.trigger == "on_notify":
        log.info("on_notify agent run complete for %s", AGENT_MD_KEY)
    elif agent_def.confirm_before_write:
        log.info("Agent run complete (propose mode): pending review for %s", CONTENT_KEY)
    else:
        log.info("Agent run complete for %s", CONTENT_KEY)
        _enqueue_index_job(CONTENT_KEY)

    if _budget is not None and tokens_used > 0:
        _budget.record_usage(USER_ID, tokens_used)
        log.info("Recorded %d tokens for user %s", tokens_used, USER_ID)

    _write_run_log(
        storage, _run_log_key, agent_def.name, "success",
        agent_def.trigger, time.monotonic() - _run_start,
    )
    _notify("agent_success", {"agent": AGENT_MD_KEY})


if __name__ == "__main__":
    main()
