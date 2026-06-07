"""IngestAgent — scheduled agent that processes files in .user/ingest/."""

from __future__ import annotations

import logging
from typing import Callable

from strands import tool
from yoloscribe_io import AgentDefinition, WikiPageMarkdownFile

from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)

_INGEST_PREFIX = ".user/ingest/"
_PROCESSED_PREFIX = ".user/ingest/processed/"


class IngestAgent(BaseAgent):
    """Agent that processes files queued in .user/ingest/ using search-driven routing.

    Tool surface:
    - ingest_list_pending(): list unprocessed files in .user/ingest/
    - ingest_read(filename): read a specific pending file
    - ingest_mark_processed(filename): move a file to .user/ingest/processed/
    - wiki_search(query): semantic search across the site to find routing targets
    - wiki_list_pages(): list all wiki page paths for structural navigation
    - wiki_read(page_path): read any wiki page for context
    - wiki_write(page_path, content): write to a wiki page
    - notify_owner(message): notify the site owner when content cannot be routed
    - http_request + any injected MCP tools
    """

    def __init__(
        self,
        agent_def: AgentDefinition,
        site: str,
        page_path: str,
        storage,
        mcp_tools: list,
        model,
        user_id: str,
        notify_fn: Callable[[str, dict, str], None],
        search: SearchBackend | None = None,
        max_page_reads: int = 10,
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
        self._read_counter: list[int] = [0]
        self._owner_instructions: str = ""

    # ── Tool surface ──────────────────────────────────────────────────────────

    @tool
    def ingest_list_pending(self) -> str:
        """List unprocessed files waiting in the ingest queue."""
        prefix = f"{self._site}/{_INGEST_PREFIX}"
        processed_prefix = f"{self._site}/{_PROCESSED_PREFIX}"
        keys = self._storage.list(prefix)
        pending = []
        for key in keys:
            # Exclude the ingest content.md, processed/ subdirectory, and agent files.
            rel = key[len(prefix):]
            if not rel or rel == "content.md" or key.startswith(processed_prefix):
                continue
            if "/.agents/" in rel or rel.startswith(".agents/"):
                continue
            pending.append(rel)
        if not pending:
            return "No pending files."
        return "\n".join(pending)

    @tool
    def ingest_read(self, filename: str) -> str:
        """Read the content of a pending ingest file by its filename."""
        filename = filename.strip().lstrip("/")
        key = f"{self._site}/{_INGEST_PREFIX}{filename}"
        content = self._storage.read(key)
        if content is None:
            return f"File not found: {filename}"
        return content

    @tool
    def ingest_mark_processed(self, filename: str) -> str:
        """Move a processed ingest file to the processed archive."""
        filename = filename.strip().lstrip("/")
        src_key = f"{self._site}/{_INGEST_PREFIX}{filename}"
        dst_key = f"{self._site}/{_PROCESSED_PREFIX}{filename}"
        content = self._storage.read(src_key)
        if content is None:
            return f"File not found: {filename}"
        self._storage.write(dst_key, content)
        self._storage.delete(src_key)
        log.info("Marked as processed: %s → %s", src_key, dst_key)
        return f"Marked as processed: {filename}"

    @tool
    def wiki_list_pages(self) -> str:
        """List all wiki page paths in this site."""
        prefix = f"{self._site}/"
        keys = self._storage.list(prefix)
        pages = []
        for key in keys:
            if not key.endswith("/content.md"):
                continue
            rel = key[len(prefix):]
            if (
                "/.agents/" in rel
                or "/.user/" in rel
                or "/.archive/" in rel
                or "/.skills/" in rel
                or rel.startswith(".agents/")
                or rel.startswith(".user/")
                or rel.startswith(".archive/")
                or rel.startswith(".skills/")
            ):
                continue
            page_path = rel[: -len("/content.md")] if rel != "content.md" else "(root)"
            pages.append(page_path)
        if not pages:
            return "No wiki pages found."
        return "Wiki pages:\n" + "\n".join(f"- {p}" for p in sorted(pages))

    @tool
    def notify_owner(self, message: str) -> str:
        """Notify the site owner that a file could not be routed automatically.

        Use this when you cannot determine a suitable destination for an ingest
        file. Do not mark the file as processed after calling this.

        Args:
            message: A plain-text explanation of what was received and why it
                     could not be routed.
        """
        self._notify("ingest_unrouted", {"message": message}, self._user_id)
        log.info("IngestAgent sent ingest_unrouted notification: %s", message[:100])
        return "Owner notified. Leave the file unprocessed so the owner can review it."

    @tool
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

    @tool
    def wiki_read(self, page_path: str) -> str:
        """Read the content of a wiki page by its page path."""
        if self._read_counter[0] >= self._max_page_reads:
            return (
                f"Error: page read limit of {self._max_page_reads} reached. "
                "Complete your task based on what you have already read."
            )
        self._read_counter[0] += 1
        page_path = page_path.strip().strip("/")
        wiki = WikiPageMarkdownFile(site=self._site, page_path=page_path, storage=self._storage)
        return wiki.read()

    @tool
    def wiki_write(self, page_path: str, content: str) -> str:
        """Write content to a wiki page."""
        page_path = page_path.strip().strip("/")
        error = self._check_scope(page_path)
        if error:
            return error
        wiki = WikiPageMarkdownFile(site=self._site, page_path=page_path, storage=self._storage)
        wiki.write(content)
        log.info("IngestAgent wrote to %s/%s", self._site, page_path)
        return f"Written to {page_path}."

    # ── Scope validation ──────────────────────────────────────────────────────

    def _check_scope(self, page_path: str) -> str | None:
        """Return an error string if page_path is out of scope, else None."""
        if not self.agent_def.scope.matches(page_path):
            return f"Access denied: {page_path} is excluded by this agent's scope settings."
        return None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _read_owner_instructions(self) -> str:
        """Read the owner's routing instructions from .user/ingest/content.md."""
        key = f"{self._site}/.user/ingest/content.md"
        content = self._storage.read(key)
        return (content or "").strip()

    def _build_system_prompt(self) -> str:
        parts = []
        if self.agent_def.description:
            parts.append(self.agent_def.description)
        if self._owner_instructions:
            parts.append(
                "The site owner has provided the following routing instructions. "
                "Follow them precisely — they take priority over your own judgement:\n\n"
                + self._owner_instructions
            )
        parts.append(
            "You process files that arrive in the ingest queue (.user/ingest/) "
            "and route each one to the appropriate wiki page using the wiki's own "
            "structure as the topic guide — no separate index to maintain.\n\n"
            "Workflow for each run:\n"
            "1. Call ingest_list_pending() to find files to process.\n"
            "2. For each pending file:\n"
            "   a. Call ingest_read(filename) to get the content.\n"
            "   b. Call wiki_search(query) with a concise summary of the content "
            "to find semantically similar wiki pages. Higher scores indicate "
            "a better semantic match.\n"
            "   c. If search results are sparse or ambiguous, call wiki_list_pages() "
            "to browse the full page structure and pick the best structural fit.\n"
            "   d. If a good destination page is found:\n"
            "      - Call wiki_read(page_path) to read its current content.\n"
            "      - Incorporate the new content appropriately (append, merge, or "
            "create a new section).\n"
            "      - Call wiki_write(page_path, updated_content) to save.\n"
            "   e. If no suitable existing page fits but the content warrants a new page:\n"
            "      - Choose a descriptive page path consistent with the wiki's "
            "naming conventions.\n"
            "      - Call wiki_write(new_page_path, content) to create it.\n"
            "   f. If you genuinely cannot determine where the content belongs:\n"
            "      - Call notify_owner(message) describing what you received and why "
            "it could not be routed. Do NOT mark the file as processed.\n"
            "   g. After successfully writing, call ingest_mark_processed(filename).\n"
        )
        return "\n\n".join(parts)

    def run(self, prompt: str) -> int:
        self._owner_instructions = self._read_owner_instructions()

        tools = [
            self.ingest_list_pending,
            self.ingest_read,
            self.ingest_mark_processed,
            self.wiki_search,
            self.wiki_list_pages,
            self.wiki_read,
            self.wiki_write,
            self.notify_owner,
        ] + self._mcp_tools

        agent = self._make_strands_agent(tools)
        task = prompt.strip() or "Process all pending ingest files according to your instructions."
        result = agent(task)
        return result.metrics.accumulated_usage.get("totalTokens", 0)
