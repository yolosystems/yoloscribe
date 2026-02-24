"""ChatAgent — handles natural-language wiki editing via Claude."""

import asyncio
import json
import logging
import os
import re
import threading
import traceback
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import anthropic
import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from pydantic import BaseModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import mypy_boto3_s3

AGENT_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


# ── Pydantic models ───────────────────────────────────────────────────────────


class AgentToCreate(BaseModel):
    name: str          # single word, S3-safe (validated)
    description: str   # the agent's prompt / purpose
    skills: list[str]  # subset of available skill names


class _Output(BaseModel):
    """Structured output schema Claude fills in on every /chat call."""
    reply: str
    updated_content: str | None = None
    agent_to_create: AgentToCreate | None = None
    agent_to_invoke: str | None = None


# ── Module-level helpers ──────────────────────────────────────────────────────


def _parse_agent_md(text: str) -> tuple[str, list[str]]:
    """Extract (description, skills) from agents.md format."""
    description = ""
    skills: list[str] = []

    desc_match = re.search(r'## Description\s*\n(.*?)(?=##|\Z)', text, re.DOTALL)
    if desc_match:
        description = desc_match.group(1).strip()

    skills_match = re.search(r'## Skills\s*\n(.*?)(?=##|\Z)', text, re.DOTALL)
    if skills_match:
        skills = [
            m.group(1).strip()
            for m in re.finditer(r'^-\s+(.+)$', skills_match.group(1), re.MULTILINE)
        ]

    return description, skills


def _load_mcp_config(raw_json: str) -> dict:
    """Parse mcp.json, substituting ${VAR} placeholders from the environment."""
    def replacer(m: re.Match) -> str:
        var_name = m.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(f"mcp.json references unset env var: {var_name!r}")
        return value

    resolved = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', replacer, raw_json)
    return json.loads(resolved)


def _unwrap_group(exc: BaseException) -> BaseException:
    """Recursively unwrap single-exception ExceptionGroups from anyio task groups."""
    if isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        return _unwrap_group(exc.exceptions[0])
    return exc


def _friendly_error(exc: BaseException) -> str:
    """Convert an exception to a user-friendly message."""
    exc = _unwrap_group(exc)
    msg = str(exc)
    if "NoSuchKey" in msg or "404" in msg:
        return "The agent or one of its skills could not be found."
    if "unset env var" in msg:
        return f"A required API token is not configured on the server ({msg})."
    if "401" in msg or "403" in msg or "Unauthorized" in msg:
        return "Authentication failed — check that the API token has the correct permissions."
    if "rate limit" in msg.lower():
        return "A rate limit was hit. Please try again in a moment."
    if isinstance(exc, FileNotFoundError) or "No such file or directory" in msg:
        return f"Could not start the MCP server — is the command installed and on PATH? ({msg})"
    return f"An unexpected error occurred: {type(exc).__name__}: {msg}"


# ── ChatAgent ─────────────────────────────────────────────────────────────────


class ChatAgent:
    """Accepts a natural-language instruction and returns an updated wiki page.

    Dependencies are injected so the agent is easy to test and swap out.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
        model: str,
    ) -> None:
        self.client = client
        self.s3 = s3
        self.bucket = bucket
        self.model = model

    def run(
        self,
        message: str,
        current_content: str,
        history: list[dict[str, str]],
        site: str,
        file_path: str = "content.md",
    ) -> tuple[str, str | None]:
        """Process a user message and return (reply, updated_content)."""
        available_skills = self._list_skills()
        available_agents = self._list_agents(site)

        skills_section = (
            "\n\nAvailable skills: " + ", ".join(available_skills)
            if available_skills
            else "\n\nAvailable skills: (none configured)"
        )

        agents_section = ""
        if available_agents:
            agents_section = (
                f"\n\nAvailable agents: {', '.join(available_agents)}\n"
                "If the user asks to invoke or run a named agent, set `agent_to_invoke` "
                "to the agent name and `reply` to a brief acknowledgement that the agent "
                "is running. Set `updated_content` to null — the agent handles that itself."
            )

        system_prompt = (
            "You are an intelligent wiki editor. "
            "The user will ask you to modify the wiki page content written in Markdown. "
            "When the user asks for a change, set `reply` to a brief friendly acknowledgement "
            "and set `updated_content` to the COMPLETE updated markdown document. "
            "If the user is just asking a question and no edit is needed, set `updated_content` to null.\n\n"
            "Current page content:\n"
            "```markdown\n"
            f"{current_content}\n"
            "```\n\n"
            f"Current file: {file_path}"
            f"{skills_section}"
            f"{agents_section}\n\n"
            "## Creating Agents\n"
            "If the user asks to create an agent, gather: a name (lowercase letters, digits, "
            "hyphens, underscores; must start with a letter or digit), a description/purpose, "
            "and which skills to include. Once you have all details, set `agent_to_create` with "
            "the validated name, description, and skills list. Set `updated_content` to null. "
            "Your reply MUST include the URL to view/edit the agent: `#/agents/{name}`."
        )

        messages = [*history, {"role": "user", "content": message}]

        result = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=system_prompt,
            messages=messages,
            output_format=_Output,
        )

        output: _Output = result.parsed_output

        if output.updated_content is not None and self.bucket:
            self._put_content(site, file_path, output.updated_content)

        if output.agent_to_create is not None and self.bucket:
            self._put_agent(site, output.agent_to_create)

        if output.agent_to_invoke is not None:
            try:
                return self._run_agent(site, output.agent_to_invoke, current_content)
            except Exception as exc:
                msg = f"Agent **{output.agent_to_invoke}** failed: {_friendly_error(exc)}"
                return msg, None

        return output.reply, output.updated_content

    # ── Agent invocation ──────────────────────────────────────────────────────

    def _run_agent(
        self, site: str, agent_name: str, content: str
    ) -> tuple[str, str | None]:
        """Sync wrapper: runs the async MCP agent in a dedicated thread+event loop.

        A new event loop is created in a background thread to avoid conflicting
        with FastAPI's event loop in the main thread.
        """
        result: list[tuple[str, str | None]] = []
        exc: list[BaseException] = []

        def target() -> None:
            try:
                result.append(
                    anyio.run(self._run_agent_async, site, agent_name, content)
                )
            except Exception as e:
                logger.error(
                    "Agent %r raised an exception:\n%s",
                    agent_name,
                    traceback.format_exc(),
                )
                exc.append(e)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join()

        if exc:
            raise exc[0]
        return result[0]

    async def _run_agent_async(
        self, site: str, agent_name: str, content: str
    ) -> tuple[str, str | None]:
        # 1. Load agent definition
        agent_md = self._get_s3_text(f"{site}/agents/{agent_name}/agents.md")
        description, skills = _parse_agent_md(agent_md)

        # 2. Load skills
        skill_instructions: list[str] = []
        mcp_server_configs: list[dict] = []
        for skill in skills:
            skill_md = self._get_s3_text(f"skills/{skill}/skill.md")
            mcp_raw = self._get_s3_text(f"skills/{skill}/mcp.json")
            mcp_cfg = _load_mcp_config(mcp_raw)
            skill_instructions.append(skill_md)
            mcp_server_configs.extend(mcp_cfg["mcpServers"].values())

        # 3. Build system prompt
        system = "\n\n".join([
            description,
            *skill_instructions,
            "Current wiki content:\n```markdown\n" + content + "\n```",
        ])

        # 4. Connect to MCP servers, collect tool schemas
        async with AsyncExitStack() as stack:
            tool_to_session: dict[str, ClientSession] = {}
            all_tools: list[dict] = []

            for cfg in mcp_server_configs:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env={**os.environ, **cfg.get("env", {})},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                tools_resp = await session.list_tools()
                for tool in tools_resp.tools:
                    all_tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema,
                    })
                    tool_to_session[tool.name] = session

            # 5. Phase 1 — agentic tool loop: gather information
            messages: list[dict] = [{
                "role": "user",
                "content": (
                    "Use the available tools to gather the information you need, "
                    "then stop so I can collect your findings."
                ),
            }]

            while True:
                response = self.client.messages.create(
                    model=self.model,
                    system=system,
                    messages=messages,
                    tools=all_tools if all_tools else anthropic.NOT_GIVEN,
                    max_tokens=8096,
                )
                messages.append({
                    "role": "assistant",
                    "content": [b.model_dump() for b in response.content],
                })

                if response.stop_reason == "end_turn":
                    break

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            session = tool_to_session.get(block.name)
                            if session is None:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"Error: unknown tool {block.name!r}",
                                    "is_error": True,
                                })
                            else:
                                try:
                                    call_result = await session.call_tool(
                                        block.name, block.input
                                    )
                                    tool_content = "\n".join(
                                        part.text if hasattr(part, "text") else str(part)
                                        for part in call_result.content
                                    )
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": tool_content,
                                    })
                                except Exception as tool_exc:
                                    logger.warning(
                                        "Tool call %r failed: %s", block.name, tool_exc
                                    )
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": f"Error: {tool_exc}",
                                        "is_error": True,
                                    })
                    messages.append({"role": "user", "content": tool_results})

        # 6. Phase 2 — structured output: produce updated wiki + reply summary.
        #    MCP sessions are closed; gathered context lives in `messages`.
        messages.append({
            "role": "user",
            "content": (
                "Based on everything you gathered, produce the final updated wiki "
                "content and a brief reply summarising what you did for the user."
            ),
        })

        final = self.client.messages.parse(
            model=self.model,
            max_tokens=8096,
            thinking={"type": "adaptive"},
            system=system,
            messages=messages,
            output_format=_Output,
        )

        output = final.parsed_output

        if output.updated_content and self.bucket:
            self._put_content(site, "content.md", output.updated_content)

        return output.reply, output.updated_content

    # ── S3 helpers ────────────────────────────────────────────────────────────

    def _list_skills(self) -> list[str]:
        if not self.bucket:
            return []
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="skills/", Delimiter="/")
        return [p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", [])]

    def _list_agents(self, site: str) -> list[str]:
        if not self.bucket:
            return []
        prefix = f"{site}/agents/"
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, Delimiter="/")
        return [
            p["Prefix"][len(prefix):].rstrip("/")
            for p in resp.get("CommonPrefixes", [])
        ]

    def _get_s3_text(self, key: str) -> str:
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def _put_agent(self, site: str, agent: AgentToCreate) -> None:
        if not AGENT_NAME_RE.match(agent.name):
            raise ValueError(f"Invalid agent name: {agent.name!r}")
        skills_list = "\n".join(f"- {s}" for s in agent.skills)
        agent_content = (
            f"# Agent: {agent.name}\n\n"
            f"## Description\n\n{agent.description}\n\n"
            f"## Skills\n\n{skills_list}\n"
        )
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"{site}/agents/{agent.name}/agents.md",
            Body=agent_content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def _put_content(self, site: str, path: str, content: str) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"{site}/{path}",
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
