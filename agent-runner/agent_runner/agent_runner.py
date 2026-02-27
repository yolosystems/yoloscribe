"""Agent runner — K8s Job entry point.

Reads an agent.md from S3, loads its MCP skills, and runs the agent against
the page's content.md, writing the result back to S3.

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

import contextlib
import json
import os
import re

import boto3
from strands import Agent
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

_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

_session = boto3.Session(profile_name=AWS_PROFILE or None)


def _s3_client():
    return _session.client("s3", region_name=AWS_REGION)


def _sm_client():
    return _session.client("secretsmanager", region_name=AWS_REGION)


def _resolve_env_vars(text: str) -> str:
    """Substitute ${VAR_NAME} in text by fetching secrets from Secrets Manager.

    Security note: Secrets Manager access is intentionally allowed here.
    This Job runs as the per-user K8s ServiceAccount (user-{USER_ID}), whose
    IRSA role is scoped exclusively to agentscribe/{USER_ID}/* secrets.
    The IAM policy prevents cross-user access at the AWS level regardless of
    what prompt the agent receives.
    """
    sm = _sm_client()

    def _fetch(match: re.Match) -> str:
        var_name = match.group(1)
        secret_id = f"agentscribe/{USER_ID}/{var_name}"
        try:
            resp = sm.get_secret_value(SecretId=secret_id)
            return resp.get("SecretString", "")
        except Exception:
            return os.environ.get(var_name, "")

    return _ENV_VAR_RE.sub(_fetch, text)


def _load_mcp_configs(s3, skill_names: list[str]) -> list[dict]:
    """Read and parse mcp.json for each skill, substituting env vars."""
    configs = []
    for skill in skill_names:
        key = f".skills/{skill}/mcp.json"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=key)
            raw = obj["Body"].read().decode("utf-8")
            raw = _resolve_env_vars(raw)
            configs.append(json.loads(raw))
        except Exception:
            pass
    return configs


def _build_mcp_clients(configs: list[dict]):
    """Build a flat list of MCPClient instances from mcp.json configs."""
    try:
        from mcp import StdioServerParameters, stdio_client
        from strands.tools.mcp import MCPClient
    except ImportError:
        return []

    clients = []
    for config in configs:
        for server_name, server_cfg in config.get("mcpServers", {}).items():
            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            env = server_cfg.get("env", {})
            params = StdioServerParameters(command=command, args=args, env=env or None)
            clients.append(MCPClient(lambda p=params: stdio_client(p)))
    return clients


def main() -> None:
    # Expose the package directory so mcp.json files can reference bundled
    # helper scripts via ${AGENT_RUNNER_HOME} (substituted by _resolve_env_vars).
    os.environ.setdefault(
        "AGENT_RUNNER_HOME", os.path.dirname(os.path.abspath(__file__))
    )

    s3 = _s3_client()

    # 1. Read and parse agent.md
    obj = s3.get_object(Bucket=BUCKET, Key=AGENT_MD_KEY)
    agent_md_text = obj["Body"].read().decode("utf-8")
    agent_def = parse_agent_md(agent_md_text)

    # 2. Load MCP configs for each skill
    mcp_configs = _load_mcp_configs(s3, agent_def.skills)

    # 3. Build MCP clients
    mcp_clients = _build_mcp_clients(mcp_configs)

    # 4. Collect tools: always include http_request, then MCP tools from skills
    tools = [http_request]
    with contextlib.ExitStack() as stack:
        for client in mcp_clients:
            stack.enter_context(client)
            try:
                tools.extend(client.list_tools_sync())
            except Exception:
                pass

        # 5. Read current content.md
        try:
            content_obj = s3.get_object(Bucket=BUCKET, Key=CONTENT_KEY)
            content = content_obj["Body"].read().decode("utf-8")
        except Exception:
            content = ""

        # 6. Run the agent
        model = AnthropicModel(model_id=MODEL_ID, max_tokens=4096)
        agent = Agent(
            system_prompt=agent_def.description,
            model=model,
            tools=tools,
            callback_handler=None,
            load_tools_from_directory=False,
        )
        full_prompt = f"{AGENT_PROMPT}\n\nCurrent content:\n```markdown\n{content}\n```"
        response = agent(full_prompt)

    # 7. Write updated content back to S3
    updated = str(response)
    s3.put_object(
        Bucket=BUCKET,
        Key=CONTENT_KEY,
        Body=updated.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    print(f"Done. Wrote {len(updated)} chars to s3://{BUCKET}/{CONTENT_KEY}")


if __name__ == "__main__":
    main()
