"""PageAgent — handles on_write and schedule triggers for wiki pages."""

from __future__ import annotations

import logging
from typing import Callable

from strands_tools import http_request
from yoloscribe_io import AgentDefinition

from ..mcp_client import AgentRunnerMCPClient
from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)

_MAX_WRITE_RETRIES = 3

# Enough of the run prompt to identify the intent, short enough to stay a
# one-liner in the owner's notification inbox.
_REASON_PROMPT_CHARS = 120


def _run_reason(agent_name: str, prompt: str) -> str:
    """Build the write reason for a write-mode run (YOL-527).

    Write mode has no LLM-supplied reason — the agent replies with markdown, not
    a tool call — so the honest intent is the instruction that triggered the run.
    Its first line is the distillate; the rest is usually context the agent was
    handed rather than a statement of purpose.
    """
    first_line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
    if len(first_line) > _REASON_PROMPT_CHARS:
        first_line = first_line[:_REASON_PROMPT_CHARS].rstrip() + "…"
    return f"{agent_name} run: {first_line}" if first_line else f"{agent_name} scheduled run"


class PageAgent(BaseAgent):
    """Agent scoped to a single wiki page.

    Tool surface (enforced programmatically):
    - page_read(): reads the bound page only
    - page_write(content): writes the bound page only
    - wiki_search(query): semantic search (read excerpts, no full page access)
    - http_request + any injected MCP tools

    The LLM cannot read or write any page other than the one this agent is
    bound to at construction time.
    """

    def __init__(
        self,
        agent_def: AgentDefinition,
        site: str,
        page_path: str,
        mcp: AgentRunnerMCPClient,
        storage,
        mcp_tools: list,
        model,
        user_id: str,
        notify_fn: Callable[[str, dict, str], None],
        search: SearchBackend | None = None,
        max_page_reads: int = 10,
        content_key: str = "",
        agent_md_key: str = "",
    ) -> None:
        super().__init__(
            agent_def=agent_def,
            site=site,
            page_path=page_path,
            storage=storage,
            mcp_tools=mcp_tools,
            model=model,
            user_id=user_id,
            notify_fn=notify_fn,
            search=search,
            max_page_reads=max_page_reads,
            mcp=mcp,
        )
        self._content_key = content_key
        self._agent_md_key = agent_md_key

    # ── Tool surface ──────────────────────────────────────────────────────────

    def page_read(self) -> str:
        """Read the current content of this wiki page."""
        return self._mcp.wiki_read(self._page_path)[0]

    def page_write(self, content: str, reason: str) -> str:
        """Write updated content to this wiki page.

        Args:
            content: The full updated markdown for the page.
            reason: One line on why this edit is being made — the intent behind
                it, not a description of the diff.
        """
        self._mcp.wiki_write(self._page_path, content, reason)
        return "Content written."

    def wiki_search(self, query: str) -> str:
        """Search the wiki semantically and return matching page excerpts."""
        results = self._mcp.search(query, limit=10)
        if not results:
            return "No matching pages found."
        lines = [
            f"**{r.page_path}** (score: {r.score:.3f})\n{r.excerpt}"
            for r in results
        ]
        return "\n\n".join(lines)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        base = (
            (self.agent_def.description or "")
            + f"\n\nYou may read and write ONLY the wiki page you are bound to. "
            "Use page_read() to read it and page_write(content) to update it.\n\n"
            "When you have finished your work, your final message must contain "
            "ONLY the complete updated markdown content — no preamble, no "
            "explanation, no summary, no commentary."
        )
        return base

    def run(self, prompt: str) -> int:
        tools = [http_request, self.page_read, self.page_write, self.wiki_search] + self._mcp_tools

        if self.agent_def.confirm_before_write:
            return self._run_propose_mode(prompt, tools)
        else:
            return self._run_write_mode(prompt, tools)

    def _run_propose_mode(self, prompt: str, tools: list) -> int:
        agent = self._make_strands_agent(tools)
        content = self._mcp.wiki_read(self._page_path)[0]
        full_prompt = (
            f"{prompt}\n\n"
            f"Current content:\n```markdown\n{content}\n```\n\n"
            "When done, reply with ONLY the updated markdown."
        )
        response = agent(full_prompt)
        updated = _strip_preamble(str(response))

        # propose_page_change stages .proposed.content.md and fires
        # confirm_page_change server-side (no separate storage write / notify).
        self._mcp.propose_page_change(self._page_path, updated, self.agent_def.name)
        log.info("Propose mode: staged %d chars for %s", len(updated), self._page_path)
        return response.metrics.accumulated_usage.get("totalTokens", 0)

    def _run_write_mode(self, prompt: str, tools: list) -> int:
        agent = self._make_strands_agent(tools)

        for attempt in range(_MAX_WRITE_RETRIES):
            content, etag = self._mcp.wiki_read(self._page_path)
            full_prompt = (
                f"{prompt}\n\n"
                f"Current content:\n```markdown\n{content}\n```\n\n"
                "When done, reply with ONLY the updated markdown."
            )
            response = agent(full_prompt)
            updated = _strip_preamble(str(response))

            if self._mcp.wiki_write(
                self._page_path, updated, _run_reason(self.agent_def.name, prompt),
                expected_etag=etag,
            ):
                return response.metrics.accumulated_usage.get("totalTokens", 0)

            if attempt == _MAX_WRITE_RETRIES - 1:
                raise RuntimeError(
                    f"Write conflict after {_MAX_WRITE_RETRIES} attempts for "
                    f"{self._content_key}"
                )
            log.warning(
                "Write conflict on attempt %d — retrying with fresh content", attempt + 1
            )
        return 0  # unreachable; satisfies type checker


def _strip_preamble(raw: str) -> str:
    """Strip any prose the model emitted before the first markdown heading."""
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("#"):
            return "\n".join(lines[idx:])
    return raw
