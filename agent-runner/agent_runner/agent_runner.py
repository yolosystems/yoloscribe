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
from strands.models.anthropic import AnthropicModel
from strands_tools import http_request

from .parse import parse_agent_md

BUCKET = os.environ["BUCKET"]
AGENT_MD_KEY = os.environ["AGENT_MD_KEY"]
CONTENT_KEY = os.environ["CONTENT_KEY"]
AGENT_PROMPT = os.environ["AGENT_PROMPT"]
USER_ID = os.environ.get("USER_ID", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_ID = os.environ.get("AGENTSCRIBE_MODEL", "claude-opus-4-6")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")

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
    """Raised when an OAuth token cannot be loaded or refreshed for a skill."""

    def __init__(self, skill_name: str, reason: str) -> None:
        super().__init__(f"OAuth error for skill '{skill_name}': {reason}")
        self.skill_name = skill_name
        self.reason = reason


def _load_and_refresh_oauth_token(skill_name: str, user_id: str, sm) -> dict:
    """Load the OAuth token for a skill from Secrets Manager.

    If the token is within 5 minutes of expiry and a refresh token is available,
    refreshes proactively and writes the updated token back to Secrets Manager.

    Raises OAuthTokenError if the token is missing or cannot be refreshed.
    """
    from mcp_oauth import OAuthError
    from mcp_oauth import refresh_access_token as _refresh
    from mcp_oauth.discovery import AuthorizationServerMetadata

    secret_id = f"agentscribe/{user_id}/oauth/{skill_name}"
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        token_data: dict = json.loads(resp["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        raise OAuthTokenError(
            skill_name,
            "No OAuth token found. Please open the Credentials panel and authenticate this skill.",
        )
    except Exception as exc:
        raise OAuthTokenError(skill_name, f"Failed to read token from Secrets Manager: {exc}")

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
            log.info("Refreshed and persisted OAuth token for skill '%s'", skill_name)
        except Exception as exc:
            log.warning("Failed to persist refreshed token for skill '%s': %s", skill_name, exc)

    return token_data


# ── MCP client construction ───────────────────────────────────────────────────


def _build_mcp_clients(skill_names: list[str], s3, sm) -> tuple[list, list[OAuthTokenError]]:
    """Build Strands MCPClient instances for all skills.

    Returns (clients, oauth_errors). OAuth errors are collected rather than
    raised so all missing tokens can be reported together.

    Remote skills with "auth": "oauth": connects via streamable HTTP with a Bearer token.
    Remote skills without "auth" (or "auth": "none"): connects via streamable HTTP unauthenticated.
    """
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient
    except ImportError:
        log.warning("MCP package not available; no MCP tools will be loaded")
        return [], []

    clients: list = []
    errors: list[OAuthTokenError] = []

    for skill in skill_names:
        key = f".skills/{skill}/mcp.json"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=key)
            config = json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as exc:
            log.warning("Failed to read mcp.json for skill '%s': %s", skill, exc)
            continue

        for server_name, server_cfg in config.get("mcpServers", {}).items():
            if "url" not in server_cfg:
                log.warning("Skipping non-remote MCP server '%s' in skill '%s'", server_name, skill)
                continue

            auth = server_cfg.get("auth", "none")
            headers: dict[str, str] = {}
            if auth == "oauth":
                try:
                    token_data = _load_and_refresh_oauth_token(skill, USER_ID, sm)
                except OAuthTokenError as exc:
                    errors.append(exc)
                    continue
                headers["Authorization"] = f"Bearer {token_data['access_token']}"

            server_url = server_cfg["url"]
            log.info("Building remote MCP client for skill '%s' server '%s' (auth=%s)", skill, server_name, auth)
            clients.append(
                MCPClient(
                    lambda u=server_url, h=headers: streamablehttp_client(u, headers=h),
                    prefix=skill,
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
        mcp_clients, oauth_errors = _build_mcp_clients(agent_def.skills, s3, sm)
        if oauth_errors:
            error_block = (
                "\n".join(
                    f"> **Agent Error** (skill `{e.skill_name}`): {e.reason}"
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

            model = AnthropicModel(
                model_id=MODEL_ID,
                max_tokens=4096,
                client_args={"max_retries": 0},
            )
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
