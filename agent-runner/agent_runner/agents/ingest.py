"""IngestAgent — scheduled agent that processes files in .user/ingest/."""

from __future__ import annotations

import logging
from typing import Callable

from strands_tools import http_request
from yoloscribe_io import AgentDefinition, KnowledgeBaseIndexMarkdownFile, WikiPageMarkdownFile

from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)

_INGEST_PREFIX = ".user/ingest/"
_PROCESSED_PREFIX = ".user/ingest/processed/"


class IngestAgent(BaseAgent):
    """Agent that runs on a schedule to process files queued in .user/ingest/.

    Tool surface:
    - ingest_list_pending(): list unprocessed files in .user/ingest/
    - ingest_read(filename): read a specific pending file
    - ingest_mark_processed(filename): move a file to .user/ingest/processed/
    - kb_index_read(): read the knowledge base topic index
    - wiki_search(query): semantic search across the site
    - wiki_read(page_path): read any wiki page (topic-scoped validation)
    - wiki_write(page_path, content): write to a wiki page that matches a ingest page topic
    - http_request + any injected MCP tools

    IngestAgent is always associated with .user/ingest and fires on schedule.
    It enforces that wiki writes are limited to pages whose top-level path
    segment matches a topic defined in .user/the ingest page.
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
        self._kb_index = KnowledgeBaseIndexMarkdownFile(site, storage)
        self._read_counter: list[int] = [0]

    # ── Tool surface ──────────────────────────────────────────────────────────

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

    def ingest_read(self, filename: str) -> str:
        """Read the content of a pending ingest file by its filename."""
        filename = filename.strip().lstrip("/")
        key = f"{self._site}/{_INGEST_PREFIX}{filename}"
        content = self._storage.read(key)
        if content is None:
            return f"File not found: {filename}"
        return content

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

    def kb_index_read(self) -> str:
        """Read the knowledge base topic index."""
        topics = self._kb_index.topics
        if not topics:
            return "The knowledge base index is empty. No topics are defined."
        return "Topics:\n" + "\n".join(f"- {t}" for t in topics)

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

    def wiki_write(self, page_path: str, content: str) -> str:
        """Write content to a wiki page. The page must match a topic in the ingest page."""
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
        # The agent.md scope field may restrict pages further via exclude globs.
        if not self.agent_def.scope.matches(page_path):
            return (
                f"Access denied: {page_path} is excluded by this agent's scope settings."
            )

        # Must match a ingest page topic (top-level segment of the page path).
        topics = self._kb_index.topics
        if topics:
            top_level = page_path.split("/")[0]
            if top_level not in topics:
                return (
                    f"Access denied: '{top_level}' is not a topic in the ingest page. "
                    f"Available topics: {', '.join(topics)}. "
                    "Add the topic to the ingest page first, or use an existing topic."
                )
        return None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        return (
            (self.agent_def.description or "")
            + "\n\n"
            + "You process files that arrive in the ingest queue (.user/ingest/) "
            "and route them to the appropriate wiki pages based on topics defined "
            "in the knowledge base index (the ingest page).\n\n"
            "Workflow for each run:\n"
            "1. Call ingest_list_pending() to find files to process.\n"
            "2. For each file: call ingest_read(filename) to get its content.\n"
            "3. Call kb_index_read() to see available topics.\n"
            "4. Determine which topic(s) the content belongs to. "
            "If the topic does not exist yet, add it to the ingest page "
            "and create the new wiki page.\n"
            "5. Use wiki_search(query) to find similar existing pages.\n"
            "6. Use wiki_read(page_path) to read relevant pages for context.\n"
            "7. Use wiki_write(page_path, content) to update or create wiki pages.\n"
            "8. Call ingest_mark_processed(filename) to archive the file.\n\n"
            "wiki_write is restricted to pages whose top-level path matches a "
            "ingest page topic. You cannot write to arbitrary wiki pages."
        )

    def run(self, prompt: str) -> int:
        tools = [
            http_request,
            self.ingest_list_pending,
            self.ingest_read,
            self.ingest_mark_processed,
            self.kb_index_read,
            self.wiki_search,
            self.wiki_read,
            self.wiki_write,
        ] + self._mcp_tools

        agent = self._make_strands_agent(tools)
        task = prompt.strip() or "Process all pending ingest files according to your instructions."
        result = agent(task)
        return result.metrics.accumulated_usage.get("totalTokens", 0)
