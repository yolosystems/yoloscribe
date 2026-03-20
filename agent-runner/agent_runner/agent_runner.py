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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time

log = logging.getLogger(__name__)

import boto3
from strands import Agent, ModelRetryStrategy
from strands_tools import http_request

from .parse import parse_agent_md

BUCKET = os.environ["BUCKET"]
AGENT_MD_KEY = os.environ["AGENT_MD_KEY"]
CONTENT_KEY = os.environ["CONTENT_KEY"]
AGENT_PROMPT = os.environ["AGENT_PROMPT"]
USER_ID = os.environ.get("USER_ID", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")

# ── Inline model registry ─────────────────────────────────────────────────────

_MODEL_REGISTRY: dict[str, tuple[str, str]] = {
    # key → (provider, model_id)
    "haiku":          ("anthropic", "claude-haiku-4-5-20251001"),
    "sonnet":         ("anthropic", "claude-sonnet-4-6"),
    "opus":           ("anthropic", "claude-opus-4-6"),
    "bedrock-haiku":  ("bedrock",   "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "bedrock-sonnet": ("bedrock",   "us.anthropic.claude-sonnet-4-6-20250514-v1:0"),
    "bedrock-opus":   ("bedrock",   "us.anthropic.claude-opus-4-6-20250514-v1:0"),
}
_DEFAULT_MODEL_KEY = "sonnet"


def _resolve_model_key(*env_vars: str) -> str:
    for var in env_vars:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return _DEFAULT_MODEL_KEY


def _build_model(model_key: str):
    provider, model_id = _MODEL_REGISTRY.get(model_key) or _MODEL_REGISTRY[_DEFAULT_MODEL_KEY]
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
    return _session.client("s3", region_name=AWS_REGION)


def _sm_client():
    return _session.client("secretsmanager", region_name=AWS_REGION)


def _enqueue_index_job(content_key: str) -> None:
    """Send an indexing job to the SQS indexing queue (best-effort; never raises)."""
    if not SQS_INDEXING_QUEUE_URL:
        return
    try:
        sqs = _session.client("sqs", region_name=AWS_REGION)
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


def _load_and_refresh_oauth_token(tool_name: str, user_id: str, sm) -> dict:
    """Load the OAuth token for a tool from Secrets Manager.

    If the token is within 5 minutes of expiry and a refresh token is available,
    refreshes proactively and writes the updated token back to Secrets Manager.

    Raises OAuthTokenError if the token is missing or cannot be refreshed.
    """
    from mcp_oauth import OAuthError
    from mcp_oauth import refresh_access_token as _refresh
    from mcp_oauth.discovery import AuthorizationServerMetadata

    secret_id = f"agentscribe/{user_id}/oauth/{tool_name}"
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        token_data: dict = json.loads(resp["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        raise OAuthTokenError(
            tool_name,
            "No OAuth token found. Please open the Tools panel and authenticate this tool.",
        )
    except Exception as exc:
        raise OAuthTokenError(tool_name, f"Failed to read token from Secrets Manager: {exc}")

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
            sm.put_secret_value(SecretId=secret_id, SecretString=json.dumps(token_data))
            log.info("Refreshed and persisted OAuth token for tool '%s'", tool_name)
        except Exception as exc:
            log.warning("Failed to persist refreshed token for tool '%s': %s", tool_name, exc)

    return token_data


def _get_aws_sso_credential_headers(user_id: str, sm) -> dict[str, str]:
    """Exchange the stored AWS SSO access token for temporary IAM credentials.

    Reads the aws-sso secret from Secrets Manager, calls sso:GetRoleCredentials,
    and returns credential headers to inject into internal MCP server requests.

    Raises OAuthTokenError if the token is missing, expired, or the exchange fails.
    """
    secret_id = f"agentscribe/{user_id}/oauth/aws-sso"
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        token_data: dict = json.loads(resp["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        raise OAuthTokenError(
            "aws-sso",
            "No AWS SSO token found. Please open the Tools panel and sign in with AWS SSO.",
        )
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


def _build_mcp_clients(skill_names: list[str], site: str, s3, sm) -> tuple[list, list[OAuthTokenError]]:
    """Build Strands MCPClient instances for all skills used by an agent.

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

    for skill_name in skill_names:
        # Step 1: load SKILL.md to get the list of tool names
        skill_key = f"{site}/.skills/{skill_name}/SKILL.md"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=skill_key)
            skill_text = obj["Body"].read().decode("utf-8")
            tool_names = _parse_skill_tools(skill_text)
        except Exception as exc:
            log.warning("Failed to read SKILL.md for skill '%s': %s", skill_name, exc)
            continue

        if not tool_names:
            log.warning("Skill '%s' has no tools defined — skipping", skill_name)
            continue

        # Step 2: for each tool, load mcp.json and build an MCP client
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
                        token_data = _load_and_refresh_oauth_token(tool_name, USER_ID, sm)
                    except OAuthTokenError as exc:
                        errors.append(exc)
                        continue
                    headers["Authorization"] = f"Bearer {token_data['access_token']}"
                elif auth == "aws-sso":
                    try:
                        headers = _get_aws_sso_credential_headers(USER_ID, sm)
                    except OAuthTokenError as exc:
                        errors.append(exc)
                        continue

                server_url = server_cfg["url"]
                log.info(
                    "Building remote MCP client for tool '%s' server '%s' (auth=%s, skill=%s)",
                    tool_name, server_name, auth, skill_name,
                )
                clients.append(
                    MCPClient(
                        lambda u=server_url, h=headers: streamablehttp_client(u, headers=h),
                        prefix=tool_name,
                    )
                )

    return clients, errors


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


def _write_error_to_content(s3, content: str, error_block: str) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=CONTENT_KEY,
        Body=(error_block + content).encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    log.info("Agent runner starting: bucket=%s agent_md=%s user=%s", BUCKET, AGENT_MD_KEY, USER_ID)

    # Expose the package directory so mcp.json files can reference bundled
    # helper scripts via ${AGENT_RUNNER_HOME} (substituted by _resolve_env_vars).
    os.environ.setdefault(
        "AGENT_RUNNER_HOME", os.path.dirname(os.path.abspath(__file__))
    )

    s3 = _s3_client()
    sm = _sm_client()

    # content holds the current page content; populated in step 2.
    # We declare it here so the top-level except block can use it for
    # error reporting even if step 2 never ran.
    content = ""

    try:
        # 1. Read and parse agent.md
        obj = s3.get_object(Bucket=BUCKET, Key=AGENT_MD_KEY)
        agent_def = parse_agent_md(obj["Body"].read().decode("utf-8"))

        # 2. Read current content.md early — we need it for error reporting too
        try:
            content_obj = s3.get_object(Bucket=BUCKET, Key=CONTENT_KEY)
            content = content_obj["Body"].read().decode("utf-8")
        except Exception:
            content = ""

        # 3. Build MCP clients; collect OAuth errors rather than raising immediately
        site = AGENT_MD_KEY.split("/")[0]
        mcp_clients, oauth_errors = _build_mcp_clients(agent_def.skills, site, s3, sm)
        if oauth_errors:
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

        # 4. Run the agent, catching any MCP or execution errors
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

            model_key = agent_def.model or _resolve_model_key(
                "AGENTSCRIBE_RUNNER_MODEL", "AGENTSCRIBE_MODEL"
            )
            model = _build_model(model_key)
            log.info("Using model key '%s' for agent '%s'", model_key, agent_def.name)
            system_prompt = (
                agent_def.description
                + "\n\n"
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
            task = AGENT_PROMPT.strip() or "Run your task as defined in your instructions."
            full_prompt = (
                f"{task}\n\n"
                f"Current content:\n```markdown\n{content}\n```\n\n"
                "When done, reply with ONLY the updated markdown. No explanations."
            )
            response = agent(full_prompt)

    except Exception as exc:
        log.error("Agent execution failed: %s", exc, exc_info=True)
        error_block = f"> **Agent Error**: The agent encountered an error during execution: {exc}\n\n"
        try:
            _write_error_to_content(s3, content, error_block)
        except Exception as write_exc:
            log.error("Additionally failed to write error to content: %s", write_exc)
        return

    # 5. Strip any preamble the model emitted before the markdown heading
    raw = str(response)
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("#"):
            raw = "\n".join(lines[idx:])
            break
    updated = raw
    s3.put_object(
        Bucket=BUCKET,
        Key=CONTENT_KEY,
        Body=updated.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    print(f"Done. Wrote {len(updated)} chars to s3://{BUCKET}/{CONTENT_KEY}")
    _enqueue_index_job(CONTENT_KEY)


if __name__ == "__main__":
    main()
