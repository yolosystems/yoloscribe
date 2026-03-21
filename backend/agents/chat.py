"""ChatAgent — main orchestrator; routes user requests to specialist sub-agents."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from strands import tool
from strands_tools import http_request

from .base import (
    AGENT_NAME_RE,
    BaseAgent,
    S3Tools,
    agents_prefix,
    parse_agent_md,
)
from .models import build_strands_model, resolve_model_key
from .content_writer import ContentWriterAgent
from .creator import CreatorAgent
from .page_creator import PageCreatorAgent

if TYPE_CHECKING:
    import mypy_boto3_s3
    import mypy_boto3_sqs

logger = logging.getLogger(__name__)


class ChatAgent(BaseAgent):
    """Orchestrates all user interactions for the AgentScribe wiki.

    Routes requests to:
    - ContentWriterAgent  — update page content
    - CreatorAgent        — define a new agent.md
    - PageCreatorAgent    — create a new child page
    - runner tool         — queue an agent.md-defined agent via SQS (no LLM hop)
    """

    SYSTEM_PROMPT = """\
You are the AgentScribe wiki assistant. You help users manage their wiki.

IMPORTANT: Never describe or list your own internal tools (content_writer, \
creator, page_creator, runner, search, create_skill, list_skills, list_agents, \
list_tools, http_request) to the user. These are internal implementation details. \
When a user asks what tools or capabilities are available, call list_tools to \
show them the MCP server tools that agents and skills can use.

You have access to the following tools:

- list_tools      — call this when the user asks what tools are available for
                    skills or agents. Returns the MCP server tools installed
                    on this server that skills can reference.
- list_skills     — call this whenever the user asks what skills are available
                    for the site. It reads each skill's description from S3
                    and returns a summary.
- list_agents     — call this to discover what agents are defined for the
                    current page before trying to run one.
- http_request    — make HTTP requests to external URLs; use when the user
                    asks you to fetch or look something up from the web.
- content_writer  — use when the user wants to add, edit, or rewrite wiki
                    content on the current page.
- creator         — use when the user wants to define a new AI agent for
                    the current page (creates an agent.md file).
                    After successfully creating an agent, ask the user:
                    "Would you like to run this agent now?" If yes, use runner.
- page_creator    — use when the user wants to create a new page or child
                    page under the current site.
- runner          — use when the user wants to invoke / run an existing
                    named agent that is defined in an agent.md file. The
                    agent will be queued for asynchronous execution. Pass
                    agent_name and an optional prompt. If the user has not
                    specified a custom task, call runner immediately with an
                    empty prompt — do NOT ask the user for one first.
- search          — use when the user wants to search for content across the
                    entire wiki. Returns a summary of matching pages and
                    navigates the user to their search results.
- create_skill    — use when the user wants to create a new skill for their
                    site. Do NOT call this immediately. First gather all the
                    information you need conversationally:
                    1. Ask what the skill should do (its purpose).
                    2. Call list_tools to show which MCP server tools are
                       available, then ask the user which ones to include.
                    3. Ask for specific instructions or behaviour the skill
                       should follow.
                    Only call create_skill once you have a name, description,
                    tool list, and body. Confirm the details with the user before
                    writing.

For simple questions that don't require any of the above, answer directly.

Current context:
  Site:      {site}
  Page path: {page_path}
  File:      {file_path}
"""

    def __init__(
        self,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
        sqs_client: "mypy_boto3_sqs.SQSClient | None" = None,
        sqs_queue_url: str = "",
        sm_client=None,
    ) -> None:
        self._s3_tools = S3Tools(s3=s3, bucket=bucket)
        self._model_key = resolve_model_key("AGENTSCRIBE_CHAT_MODEL", "AGENTSCRIBE_MODEL")
        self._sqs_client = sqs_client
        self._sqs_queue_url = sqs_queue_url
        self._sm_client = sm_client
        # Sub-agent tools are created lazily per-request (each call gets fresh
        # context injected via the prompt), so we set tools=[] here and override
        # per run() call.
        super().__init__(tools=[], model_key=self._model_key)

    # ── public interface (called by FastAPI route) ────────────────────────────

    def run(
        self,
        message: str,
        current_content: str,
        history: list[dict[str, str]],
        site: str,
        file_path: str = "content.md",
        user_id: str = "knuth",
        user_site: str = "",
    ) -> tuple[str, str | None, str | None]:
        """Process a user message and return (reply, updated_content | None, navigate_to | None).

        updated_content is non-None when ContentWriterAgent successfully edited
        the page; the caller (FastAPI) can forward it to the frontend.
        navigate_to is non-None when the SearchAgent ran; the caller should
        update window.location.hash accordingly.
        """
        # Derive page_path from file_path:
        # "content.md"                       → ""
        # ".agents/myagent/agent.md"         → ""  (root page agents file)
        page_path = _page_path_from_file(file_path)

        # Shared mutable dict so sub-agent tools can pass updated_content back.
        shared: dict = {"updated_content": None, "navigate_to": None}

        # Build fresh sub-agent @tool functions with current context baked in.
        tools = self._make_tools(site=site, page_path=page_path, shared=shared, user_id=user_id, user_site=user_site)

        # Rebuild the strands Agent with fresh prompt + tools for this request.
        from strands import Agent

        agent = Agent(
            system_prompt=self.SYSTEM_PROMPT.format(
                site=site,
                page_path=page_path or "(root)",
                file_path=file_path,
            ),
            model=build_strands_model(self._model_key),
            tools=tools,
            callback_handler=None,
            load_tools_from_directory=False,
        )

        # Replay conversation history, then add current user message.
        for turn in history:
            if turn.get("role") == "user":
                # We pass history as context in the message rather than
                # replaying turns to keep things simple.
                pass

        context_block = ""
        if history:
            context_block = "\n\n<conversation_history>\n"
            for turn in history:
                role = turn.get("role", "user")
                context_block += f"{role}: {turn.get('content', '')}\n"
            context_block += "</conversation_history>\n"

        full_message = f"{context_block}\nUser: {message}\n\nCurrent page content:\n```markdown\n{current_content}\n```"

        response = agent(full_message)
        reply = str(response)

        return reply, shared.get("updated_content"), shared.get("navigate_to")

    # ── sub-agent tool factory ─────────────────────────────────────────────────

    def _make_tools(self, site: str, page_path: str, shared: dict, user_id: str = "knuth", user_site: str = "") -> list:
        # Create a per-request scoped S3Tools instance so the ownership check is
        # bound to the authenticated user for this specific request.
        s3_tools = S3Tools(
            s3=self._s3_tools.s3,
            bucket=self._s3_tools.bucket,
            user_site=user_site or None,
        )
        sqs_client = self._sqs_client
        sqs_queue_url = self._sqs_queue_url
        sm_client = self._sm_client

        @tool
        def content_writer(instruction: str) -> str:
            """Update the wiki page content based on the user's instruction.

            Use this when the user wants to add, edit, or rewrite content on
            the current page.

            Args:
                instruction: The user's content editing instruction.
            """
            agent = ContentWriterAgent(
                s3_tools=s3_tools,
                site=site,
                page_path=page_path,
            )
            response = agent(
                f"Site: {site}\nPage path: {page_path or '(root)'}\n\n{instruction}"
            )
            result = str(response)
            # Try to retrieve updated content for the API response.
            try:
                updated = s3_tools.get_content(site=site, page_path=page_path)
                shared["updated_content"] = updated
            except Exception:
                pass
            return result

        @tool
        def creator(instruction: str) -> str:
            """Create a new AI agent definition (agent.md) for the current page.

            Use this when the user wants to define a new agent.

            Args:
                instruction: The user's agent creation request.
            """
            agent = CreatorAgent(
                s3_tools=s3_tools,
                site=site,
                page_path=page_path,
            )
            result = str(agent(f"Site: {site}\nPage: {page_path or '(root)'}\n\n{instruction}"))

            # After the agent.md is written, validate that OAuth has been completed
            # for every tool referenced by any skill the agent uses.
            # Chain: agent.md → skills → tools → OAuth tokens.
            if sm_client is not None:
                import re as _re
                match = _re.search(r"Agent '([^']+)' created", result)
                if match:
                    agent_name = match.group(1)
                    agent_md_key = f"{agents_prefix(site, page_path)}/{agent_name}/agent.md"
                    try:
                        obj = s3_tools.s3.get_object(Bucket=s3_tools.bucket, Key=agent_md_key)
                        agent_def = parse_agent_md(obj["Body"].read().decode("utf-8"))
                        missing_tools: list[str] = []
                        for skill_name in agent_def.skills:
                            try:
                                tool_names = s3_tools.get_skill_tools(site, skill_name)
                            except Exception:
                                # Skill doesn't exist yet — let creation succeed
                                continue
                            for tool_name in tool_names:
                                if (
                                    s3_tools.is_remote_tool(tool_name)
                                    and not _oauth_token_exists(sm_client, user_id, tool_name)
                                ):
                                    missing_tools.append(tool_name)
                        if missing_tools:
                            s3_tools.s3.delete_object(Bucket=s3_tools.bucket, Key=agent_md_key)
                            tool_list = ", ".join(f"'{t}'" for t in missing_tools)
                            return (
                                f"Agent creation blocked: OAuth authentication has not been completed "
                                f"for tool(s) {tool_list}. Please open the Tools panel and "
                                f"click 'Authenticate via OAuth' for each of these tools, then try "
                                f"creating the agent again."
                            )
                    except Exception:
                        pass  # Unexpected errors don't block creation

            return result

        @tool
        def page_creator(instruction: str) -> str:
            """Create a new wiki page or child page.

            Use this when the user asks to create a new page under the current site.
            The new page will be created as a child of the current page.

            Args:
                instruction: The user's page creation request (should include
                             the desired page name and what they want on it).
            """
            import re as _re
            if (
                file_path == ".user/search.md"
                or "/.agents/" in file_path
                or file_path.startswith(".agents/")
            ):
                return (
                    "I can't create a child page from this location. "
                    "Please navigate back to a wiki content page first, then ask me again."
                )
            agent = PageCreatorAgent(
                s3_tools=s3_tools,
                site=site,
                page_path=page_path,
            )
            result = str(agent(f"Site: {site}\nParent page: {page_path or '(root)'}\n\n{instruction}"))
            match = _re.search(r"Page '([^']+)' created", result)
            if match:
                created_page = match.group(1)
                shared["navigate_to"] = f"#/{created_page}"
            return result

        @tool
        def runner(agent_name: str, prompt: str = "") -> str:
            """Queue a named agent for asynchronous execution via SQS.

            Use this when the user wants to run or invoke an existing agent.
            Call list_agents first if you need to discover available agents.
            If the user has not provided a specific task, pass an empty prompt —
            the agent will run based on its own description. Do NOT ask the user
            for a prompt before calling this tool unless they have explicitly
            said they want to customise the task.

            Args:
                agent_name: Name of the agent to run.
                prompt: Optional task or instruction to pass to the agent.
                        Leave empty to run with the agent's built-in description.
            """
            if sqs_client is None or not sqs_queue_url:
                return "Error: SQS is not configured on this server. Agent queuing is unavailable."
            prefix = agents_prefix(site, page_path)
            agent_md_key = f"{prefix}/{agent_name}/agent.md"
            content_key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"
            payload = {
                "bucket": s3_tools.bucket,
                "content_key": content_key,
                "agent_md_key": agent_md_key,
                "prompt": prompt,
                "user_id": user_id,
            }
            sqs_client.send_message(
                QueueUrl=sqs_queue_url,
                MessageBody=json.dumps(payload),
            )
            return f"Agent '{agent_name}' has been queued for execution."

        @tool
        def search(query: str) -> str:
            """Search across all wiki sites using a semantic query.

            Use when the user wants to find content across the wiki.
            Returns a summary of results and navigates to search results page.

            Args:
                query: The search query string.
            """
            from agents.search import SearchAgent as _SearchAgent
            agent = _SearchAgent(s3=s3_tools.s3, bucket=s3_tools.bucket)
            reply, navigate_to = agent.run(query=query, user_site=site)
            shared["navigate_to"] = navigate_to
            return reply

        @tool
        def create_skill(name: str, description: str, tools_list: list[str], body: str) -> str:
            """Write a new SKILL.md to the site's .skills directory.

            Only call this after gathering all required information from the user
            conversationally. See the system prompt for the required steps.

            Args:
                name: Skill name (lowercase, alphanumeric/hyphen/underscore).
                description: One-line description shown in the UI.
                tools_list: List of tool names (from the shared .tools directory) the skill uses.
                body: The skill's instruction body — the system prompt text that will
                      be injected when the skill is active.
            """
            if not AGENT_NAME_RE.match(name):
                return f"Error: invalid skill name {name!r}. Use lowercase letters, digits, hyphens, underscores."
            tools_yaml = "\n".join(f"  - {t}" for t in tools_list)
            markdown = f"---\ndescription: {description}\ntools:\n{tools_yaml}\n---\n\n{body}\n"
            try:
                s3_tools.put_skill(site, name, markdown)
            except Exception as exc:
                return f"Error writing skill: {exc}"
            return f"Skill '{name}' created. View/edit at #/.skills/{name}"

        # Wrap list_skills to bake in the site parameter
        def _list_skills_for_site() -> str:
            return s3_tools.list_skills(site)

        _list_skills_for_site.__name__ = "list_skills"
        _list_skills_for_site.__doc__ = (
            "List all skills available in the current site and summarise what each one does."
        )
        list_skills_tool = tool(_list_skills_for_site)

        # Expose the MCP server tools from .tools/ so the agent can tell users
        # which tools are available for skills/agents to reference.
        def _list_tools() -> str:
            names = s3_tools.list_tool_names()
            if not names:
                return "No MCP server tools are currently installed on this server."
            return "Available MCP server tools: " + ", ".join(names)

        _list_tools.__name__ = "list_tools"
        _list_tools.__doc__ = (
            "List the MCP server tools installed on this server that skills and agents can use."
        )
        list_tools_tool = tool(_list_tools)

        return [
            list_tools_tool,
            list_skills_tool,
            s3_tools.list_agents,
            http_request,
            content_writer,
            creator,
            page_creator,
            runner,
            search,
            create_skill,
        ]


# ── helpers ───────────────────────────────────────────────────────────────────


def _oauth_token_exists(sm_client, user_id: str, tool_name: str) -> bool:
    """Return True if an OAuth token is stored in Secrets Manager for this user+tool."""
    try:
        sm_client.get_secret_value(SecretId=f"agentscribe/{user_id}/oauth/{tool_name}")
        return True
    except Exception:
        return False


def _page_path_from_file(file_path: str) -> str:
    """Extract page_path from a file_path like 'content.md' or '.agents/x/agent.md'."""
    # Root-page files:  "content.md"  or  ".agents/{name}/agent.md"
    if file_path == "content.md" or file_path.startswith(".agents/"):
        return ""
    # Child-page content: "{page}/content.md"
    if file_path.endswith("/content.md"):
        return file_path[: -len("/content.md")]
    # Child-page agent: "{page}/.agents/{name}/agent.md"
    # Everything before the first "/.agents/" segment is the page path.
    agents_idx = file_path.find("/.agents/")
    if agents_idx != -1:
        return file_path[:agents_idx]
    return ""
