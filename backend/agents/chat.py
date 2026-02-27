"""ChatAgent — main orchestrator; routes user requests to specialist sub-agents."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from strands import tool
from strands_tools import http_request

from .base import BaseAgent, S3Tools, DEFAULT_MODEL
from .content_writer import ContentWriterAgent
from .creator import CreatorAgent
from .page_creator import PageCreatorAgent
from .runner import RunnerAgent

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
    - RunnerAgent         — queue an agent.md-defined agent via SQS
    """

    SYSTEM_PROMPT = """\
You are the AgentScribe wiki assistant. You help users manage their wiki.

You have access to the following tools and specialist sub-agents:

- list_skills     — call this whenever the user asks what skills are available
                    on the server. It reads each skill's description from S3
                    and returns a summary.
- http_request    — make HTTP requests to external URLs; use when the user
                    asks you to fetch or look something up from the web.
- content_writer  — use when the user wants to add, edit, or rewrite wiki
                    content on the current page.
- creator         — use when the user wants to define a new AI agent for
                    the current page (creates an agent.md file).
                    After successfully creating an agent, ask the user:
                    "Would you like to run this agent now?" If yes, delegate to runner.
- page_creator    — use when the user wants to create a new page or child
                    page under the current site.
- runner          — use when the user wants to invoke / run an existing
                    named agent that is defined in an agent.md file.

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
        model_id: str = DEFAULT_MODEL,
        sqs_client: "mypy_boto3_sqs.SQSClient | None" = None,
        sqs_queue_url: str = "",
    ) -> None:
        self._s3_tools = S3Tools(s3=s3, bucket=bucket)
        self._model_id = model_id
        self._sqs_client = sqs_client
        self._sqs_queue_url = sqs_queue_url
        # Sub-agent tools are created lazily per-request (each call gets fresh
        # context injected via the prompt), so we set tools=[] here and override
        # per run() call.
        super().__init__(tools=[], model_id=model_id)

    # ── public interface (called by FastAPI route) ────────────────────────────

    def run(
        self,
        message: str,
        current_content: str,
        history: list[dict[str, str]],
        site: str,
        file_path: str = "content.md",
        user_id: str = "knuth",
    ) -> tuple[str, str | None]:
        """Process a user message and return (reply, updated_content | None).

        updated_content is non-None when ContentWriterAgent successfully edited
        the page; the caller (FastAPI) can forward it to the frontend.
        """
        # Derive page_path from file_path:
        # "content.md"                       → ""
        # ".agents/myagent/agent.md"         → ""  (root page agents file)
        page_path = _page_path_from_file(file_path)

        # Shared mutable dict so sub-agent tools can pass updated_content back.
        shared: dict = {"updated_content": None}

        # Build fresh sub-agent @tool functions with current context baked in.
        tools = self._make_tools(site=site, page_path=page_path, shared=shared, user_id=user_id)

        # Rebuild the strands Agent with fresh prompt + tools for this request.
        from strands import Agent
        from strands.models.anthropic import AnthropicModel

        agent = Agent(
            system_prompt=self.SYSTEM_PROMPT.format(
                site=site,
                page_path=page_path or "(root)",
                file_path=file_path,
            ),
            model=AnthropicModel(model_id=self._model_id, max_tokens=4096),
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

        return reply, shared.get("updated_content")

    # ── sub-agent tool factory ─────────────────────────────────────────────────

    def _make_tools(self, site: str, page_path: str, shared: dict, user_id: str = "knuth") -> list:
        s3_tools = self._s3_tools
        model_id = self._model_id

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
                model_id=model_id,
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
                model_id=model_id,
                site=site,
                page_path=page_path,
            )
            return str(agent(f"Site: {site}\nPage: {page_path or '(root)'}\n\n{instruction}"))

        @tool
        def page_creator(instruction: str) -> str:
            """Create a new wiki page or child page.

            Use this when the user asks to create a new page under the current site.

            Args:
                instruction: The user's page creation request (should include
                             the desired page name/path).
            """
            agent = PageCreatorAgent(
                s3_tools=s3_tools,
                model_id=model_id,
                site=site,
                page_path=page_path,
            )
            return str(agent(f"Site: {site}\nParent page: {page_path or '(root)'}\n\n{instruction}"))

        @tool
        def runner(instruction: str) -> str:
            """Invoke (queue) a named agent defined in an agent.md file.

            Use this when the user asks to run or invoke an existing agent
            by name.

            Args:
                instruction: The user's request, including which agent to run
                             and what task to give it.
            """
            if self._sqs_client is None or not self._sqs_queue_url:
                return "Error: SQS is not configured on this server. Agent queuing is unavailable."
            agent = RunnerAgent(
                s3_tools=s3_tools,
                sqs_queue_url=self._sqs_queue_url,
                sqs_client=self._sqs_client,
                model_id=model_id,
                user_id=user_id,
                site=site,
                page_path=page_path,
            )
            return str(agent(f"Site: {site}\nPage: {page_path or '(root)'}\n\n{instruction}"))

        return [s3_tools.list_skills, http_request, content_writer, creator, page_creator, runner]


# ── helpers ───────────────────────────────────────────────────────────────────


def _page_path_from_file(file_path: str) -> str:
    """Extract page_path from a file_path like 'content.md' or '.agents/x/agent.md'."""
    # Root-page files:  "content.md"  or  ".agents/{name}/agent.md"
    if file_path in ("content.md",) or file_path.startswith(".agents/"):
        return ""
    # Child-page files: "{page}/content.md"  or  "{page}/.agents/{name}/agent.md"
    parts = file_path.split("/")
    if parts[-1] in ("content.md", "agent.md"):
        # Drop the filename (and .agents/{name} if present)
        candidate = "/".join(p for p in parts[:-1] if not p.startswith("."))
        return candidate.strip("/")
    return ""
