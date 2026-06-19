"""PageAgent — handles on_write and schedule triggers for wiki pages."""

from __future__ import annotations

import json
import logging
from typing import Callable

from strands_tools import http_request
from yoloscribe_io import AgentDefinition, WikiPageMarkdownFile

from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)

_MAX_WRITE_RETRIES = 3


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
        wiki: WikiPageMarkdownFile,
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
        )
        self._wiki = wiki
        self._content_key = content_key
        self._agent_md_key = agent_md_key

    # ── Tool surface ──────────────────────────────────────────────────────────

    def page_read(self) -> str:
        """Read the current content of this wiki page."""
        return self._wiki.read()

    def page_write(self, content: str) -> str:
        """Write updated content to this wiki page."""
        self._wiki.write(content)
        return "Content written."

    def wiki_search(self, query: str) -> str:
        """Search the wiki semantically and return matching page excerpts."""
        results = self._search.search(query, self._site, limit=10)
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
        content = self._wiki.read()
        full_prompt = (
            f"{prompt}\n\n"
            f"Current content:\n```markdown\n{content}\n```\n\n"
            "When done, reply with ONLY the updated markdown."
        )
        response = agent(full_prompt)
        updated = _strip_preamble(str(response))

        proposed_key = self._content_key[:-len("content.md")] + ".proposed.content.md"
        meta_key = self._content_key[:-len("content.md")] + ".proposed.content.meta.json"
        self._storage.write(proposed_key, updated)
        self._storage.write(meta_key, json.dumps({"agent_md_key": self._agent_md_key}))
        log.info("Propose mode: wrote %d chars to %s", len(updated), proposed_key)
        self._notify(
            "confirm_page_change",
            {
                "agent": self.agent_def.name,
                "content_key": self._content_key,
                "proposed_key": proposed_key,
            },
            self._user_id,
        )
        return response.metrics.accumulated_usage.get("totalTokens", 0)

    def _run_write_mode(self, prompt: str, tools: list) -> int:
        agent = self._make_strands_agent(tools)

        for attempt in range(_MAX_WRITE_RETRIES):
            content, etag = self._wiki.read_with_etag()
            full_prompt = (
                f"{prompt}\n\n"
                f"Current content:\n```markdown\n{content}\n```\n\n"
                "When done, reply with ONLY the updated markdown."
            )
            response = agent(full_prompt)
            updated = _strip_preamble(str(response))

            if self._wiki.write_conditional(updated, etag, user_id=self._user_id):
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
