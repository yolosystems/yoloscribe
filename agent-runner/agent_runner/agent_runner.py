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
from strands import Agent, ModelRetryStrategy
from strands_tools import http_request

from .parse import AgentDefinitionError, parse_agent_md

BUCKET = os.environ["BUCKET"]
AGENT_MD_KEY = os.environ["AGENT_MD_KEY"]
CONTENT_KEY = os.environ["CONTENT_KEY"]
AGENT_PROMPT = os.environ["AGENT_PROMPT"]
USER_ID = os.environ.get("USER_ID", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
SQS_ENDPOINT_URL = os.environ.get("SQS_ENDPOINT_URL", "")
LOCAL_MODE: bool = os.environ.get("LOCAL_MODE", "").lower() in ("1", "true", "yes")
LOCAL_MCP_CONFIG_PATH: str = os.environ.get("LOCAL_MCP_CONFIG_PATH", "/app/local-mcp-servers.json")
AGENT_RUNNER_MAX_PAGE_READS: int = int(os.environ.get("AGENT_RUNNER_MAX_PAGE_READS", "10"))

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

    secret_key = f"yoloscribe/{user_id}/oauth/{tool_name}"
    raw = store.get(secret_key)
    if raw is None:
        raise OAuthTokenError(
            tool_name,
            "No OAuth token found. Please open the Tools panel and authenticate this tool.",
        )
    try:
        token_data: dict = json.loads(raw)
    except Exception as exc:
        raise OAuthTokenError(tool_name, f"Failed to read token: {exc}")

    # Proactive refresh: refresh if the token expires within 5 minutes
    expires_at = token_data.get("expires_at", 0)
    if time.time() > expires_at - 300:
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise OAuthTokenError(
                skill_name,
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
            raise OAuthTokenError(skill_name, f"Token refresh failed: {exc}")

        token_data["access_token"] = new_tokens.get("access_token", token_data["access_token"])
        if "refresh_token" in new_tokens:
            token_data["refresh_token"] = new_tokens["refresh_token"]
        token_data["expires_at"] = int(time.time()) + int(new_tokens.get("expires_in", 3600))
        if "scope" in new_tokens:
            token_data["scope"] = new_tokens["scope"]

        try:
            store.put(secret_key, json.dumps(token_data))
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


# ── Frontmatter parser ────────────────────────────────────────────────────────


def _parse_skill_tools(text: str) -> list[str]:
    """Extract the tools list from a SKILL.md frontmatter block.

    Returns an empty list if parsing fails or the tools key is absent.
    """
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    fm_text = text[3:end].strip()
    tools: list[str] = []
    in_tools = False
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("tools:"):
            in_tools = True
            value = stripped[len("tools:"):].strip()
            if value:  # inline list or scalar
                tools = [v.strip().strip("\"'") for v in value.strip("[]").split(",") if v.strip()]
            continue
        if in_tools and stripped.startswith("- "):
            tools.append(stripped[2:].strip().strip("\"'"))
        elif ":" in stripped and not stripped.startswith("- "):
            in_tools = False
    return tools


# ── MCP client construction ───────────────────────────────────────────────────


def _collect_tool_names(skill_names: list[str], site: str, s3) -> list[str]:
    """Resolve skill names → tool names via SKILL.md frontmatter."""
    tool_names: list[str] = []
    seen: set[str] = set()
    for skill_name in skill_names:
        skill_key = f"{site}/.skills/{skill_name}/SKILL.md"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=skill_key)
            skill_text = obj["Body"].read().decode("utf-8")
            names = _parse_skill_tools(skill_text)
        except Exception as exc:
            log.warning("Failed to read SKILL.md for skill '%s': %s", skill_name, exc)
            continue
        if not names:
            log.warning("Skill '%s' has no tools defined — skipping", skill_name)
            continue
        for name in names:
            if name not in seen:
                seen.add(name)
                tool_names.append(name)
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


def _build_local_mcp_clients(skill_names: list[str], site: str, s3) -> tuple[list, list]:
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

    required_tool_names = set(_collect_tool_names(skill_names, site, s3))
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


def _build_remote_mcp_clients(skill_names: list[str], site: str, s3, store) -> tuple[list, list[OAuthTokenError]]:
    """Build HTTP MCPClient instances for all skills used by an agent.

    Resolution chain: agent.md → skill names → SKILL.md (tools list) → .tools/{name}/mcp.json

    Returns (clients, oauth_errors). OAuth errors are collected rather than
    raised so all missing tokens can be reported together.

    Remote tools with "auth": "oauth": connects via streamable HTTP with a Bearer token.
    Remote tools without "auth" (or "auth": "none"): connects via streamable HTTP unauthenticated.
    """
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient
    except ImportError:
        log.warning("MCP package not available; no MCP tools will be loaded")
        return [], []

    clients: list = []
    errors: list[OAuthTokenError] = []
    tool_names = _collect_tool_names(skill_names, site, s3)

    for tool_name in tool_names:
        tool_key = f".tools/{tool_name}/mcp.json"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=tool_key)
            config = json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as exc:
            log.warning("Failed to read mcp.json for tool '%s': %s", tool_name, exc)
            continue

        for server_name, server_cfg in config.get("mcpServers", {}).items():
            if "url" not in server_cfg:
                log.warning("Skipping non-remote MCP server '%s' in tool '%s'", server_name, tool_name)
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


def _build_mcp_clients(skill_names: list[str], site: str, s3, store) -> tuple[list, list[OAuthTokenError]]:
    """Build Strands MCPClient instances for all skills used by an agent.

    In LOCAL_MODE: reads local-mcp-servers.json and builds STDIO clients — no S3 tool
    lookup, no OAuth. In production: reads .tools/{name}/mcp.json from S3 and builds
    HTTP clients with OAuth/SSO auth as configured.
    """
    if LOCAL_MODE:
        return _build_local_mcp_clients(skill_names, site, s3)
    return _build_remote_mcp_clients(skill_names, site, s3, store)


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


def _write_notification(s3, site: str, message: str) -> None:
    """Prepend a notification entry to {site}/.user/notifications.md."""
    import datetime
    key = f"{site}/.user/notifications.md"
    now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"## Agent Error — {now}\n\n{message}\n\n---\n\n"
    try:
        existing = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except Exception:
        existing = ""
    combined = entry + existing
    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=combined.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
    except Exception as exc:
        log.error("Failed to write notification for site %s: %s", site, exc)


def _write_error_to_content(s3, content: str, error_block: str) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=CONTENT_KEY,
        Body=(error_block + content).encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def _get_content_with_etag(s3, key: str) -> tuple[str, str | None]:
    """Read content.md and return (content, etag). Returns ("", None) on missing key."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return obj["Body"].read().decode("utf-8"), obj["ETag"]
    except Exception:
        return "", None


def _put_content_conditional(s3, key: str, content: str, etag: str | None) -> bool:
    """PUT with If-Match if an etag is available. Returns True on success, False on 412."""
    kwargs: dict = {"IfMatch": etag} if etag else {}
    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            **kwargs,
        )
        return True
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            return False
        raise


def main() -> None:
    configure_logging()
    log.info("Agent runner starting: bucket=%s agent_md=%s user=%s", BUCKET, AGENT_MD_KEY, USER_ID)

    # Expose the package directory so mcp.json files can reference bundled
    # helper scripts via ${AGENT_RUNNER_HOME} (substituted by _resolve_env_vars).
    os.environ.setdefault(
        "AGENT_RUNNER_HOME", os.path.dirname(os.path.abspath(__file__))
    )

    s3 = _s3_client()
    store = _make_secrets_store(s3)

    # content holds the current page content; populated in step 2.
    # We declare it here so the top-level except block can use it for
    # error reporting even if step 2 never ran.
    content = ""

    try:
        # 1. Read and parse agent.md
        obj = s3.get_object(Bucket=BUCKET, Key=AGENT_MD_KEY)
        try:
            agent_def = parse_agent_md(obj["Body"].read().decode("utf-8"))
        except AgentDefinitionError as exc:
            log.error("Invalid agent.md at %s: %s", AGENT_MD_KEY, exc)
            site = AGENT_MD_KEY.split("/")[0]
            _write_notification(
                s3, site,
                f"**Invalid agent definition** (`{AGENT_MD_KEY}`):\n\n{exc}",
            )
            return

        # 2. Build MCP clients once — not repeated on write-conflict retries
        site = AGENT_MD_KEY.split("/")[0]
        mcp_clients, oauth_errors = _build_mcp_clients(agent_def.skills, site, s3, store)
        if oauth_errors:
            content, _ = _get_content_with_etag(s3, CONTENT_KEY)
            error_block = (
                "\n".join(
                    f"> **Agent Error** (tool `{e.tool_name}`): {e.reason}"
                    for e in oauth_errors
                )
                + "\n\n"
            )
            _write_error_to_content(s3, content, error_block)
            log.error("Aborting: OAuth token error(s): %s", [str(e) for e in oauth_errors])
            return

        # 3. Build agent once — MCP tools, model, and system prompt are stable across retries
        tools = [http_request]
        with contextlib.ExitStack() as stack:
            for client in mcp_clients:
                try:
                    stack.enter_context(client)
                except Exception as exc:
                    log.warning("MCP client failed to start: %s", exc)
                    continue
                try:
                    mcp_tools = client.list_tools_sync()
                    _sanitize_tool_names(mcp_tools)
                    tools.extend(mcp_tools)
                    log.info("Loaded %d tools from MCP client", len(mcp_tools))
                except Exception as exc:
                    log.warning("Failed to load tools from MCP client: %s", exc)

            tools = _apply_read_limit(tools, AGENT_RUNNER_MAX_PAGE_READS)
            log.info("Page read limit: %d wiki_read calls per run", AGENT_RUNNER_MAX_PAGE_READS)

            model_key = agent_def.model or _resolve_model_key(
                "YOLOSCRIBE_RUNNER_MODEL", "YOLOSCRIBE_MODEL"
            )
            model = _build_model(model_key)
            log.info("Using model key '%s' for agent '%s'", model_key, agent_def.name)
            system_prompt = (
                agent_def.description
                + "\n\n"
                + f"You may read at most {AGENT_RUNNER_MAX_PAGE_READS} wiki pages per run "
                f"(enforced by the runtime). Prioritise the pages most relevant to your task.\n\n"
                + "IMPORTANT: When you have finished your work, your final message must contain "
                "ONLY the complete updated markdown content — no preamble, no explanation, no "
                "summary, no commentary. Output the raw markdown and nothing else."
            )
            agent = Agent(
                system_prompt=system_prompt,
                model=model,
                tools=tools,
                callback_handler=None,
                load_tools_from_directory=False,
                retry_strategy=ModelRetryStrategy(
                    max_attempts=8,
                    initial_delay=10,
                    max_delay=120,
                ),
            )

            # 4. Read → run → conditional write, retrying on write conflict
            _MAX_WRITE_RETRIES = 3
            for attempt in range(_MAX_WRITE_RETRIES):
                # Read fresh content + ETag on every attempt
                content, etag = _get_content_with_etag(s3, CONTENT_KEY)

                task = AGENT_PROMPT.strip() or "Run your task as defined in your instructions."
                full_prompt = (
                    f"{task}\n\n"
                    f"Current content:\n```markdown\n{content}\n```\n\n"
                    "When done, reply with ONLY the updated markdown. No explanations."
                )
                response = agent(full_prompt)

                # 5. Strip any preamble the model emitted before the markdown heading
                raw = str(response)
                lines = raw.splitlines()
                for idx, line in enumerate(lines):
                    if line.startswith("#"):
                        raw = "\n".join(lines[idx:])
                        break
                updated = raw

                if _put_content_conditional(s3, CONTENT_KEY, updated, etag):
                    break

                if attempt == _MAX_WRITE_RETRIES - 1:
                    log.error(
                        "Write conflict after %d attempts for %s — giving up",
                        _MAX_WRITE_RETRIES, CONTENT_KEY,
                    )
                    error_block = (
                        "> **Agent Error**: Could not save — the page was modified by "
                        "another writer on every attempt. Please try again.\n\n"
                    )
                    _write_error_to_content(s3, content, error_block)
                    return

                log.warning(
                    "Write conflict on attempt %d for %s — retrying with fresh content",
                    attempt + 1, CONTENT_KEY,
                )

    except Exception as exc:
        log.error("Agent execution failed: %s", exc, exc_info=True)
        error_block = f"> **Agent Error**: The agent encountered an error during execution: {exc}\n\n"
        try:
            _write_error_to_content(s3, content, error_block)
        except Exception as write_exc:
            log.error("Additionally failed to write error to content: %s", write_exc)
        return

    log.info("Agent run complete: wrote %d chars to s3://%s/%s", len(updated), BUCKET, CONTENT_KEY)
    _enqueue_index_job(CONTENT_KEY)


if __name__ == "__main__":
    main()
