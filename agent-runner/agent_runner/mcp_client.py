"""AgentRunnerMCPClient — the runner's typed client for YoloScribe's own MCP tools (YOL-502).

This is the seam that turns the agent-runner into an **MCP client** instead of a
direct-S3 writer (re-arch Phase 1 / P1.2 / R1). Every wiki read/write, ingest
operation, proposal, search, and notification the agent classes and `main()`
perform goes through the YoloScribe MCP server using a scoped **run token** —
the same surface a third-party runtime would use — so `_check_scope` enforces
the per-agent-type path floor server-side and mutation events/signals fire
server-side for free.

Three implementations:
- ``AgentRunnerMCPClient`` — the abstract interface, driven by exactly what
  ``PageAgent``/``IngestAgent``/``main()`` call.
- ``HttpMCPClient`` — the real one: a Strands ``MCPClient`` over streamable-HTTP
  to ``MCP_URL`` with ``Authorization: Bearer <run_token>``, calling named tools
  via ``call_tool_sync``. (Protocol result-parsing; verify with a live smoke
  test when the runner wiring lands.)
- ``FakeMCPClient`` — an in-memory double for unit tests and the conformance
  harness, with real etag/conflict simulation.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    page_path: str
    score: float
    excerpt: str


class WriteConflict(Exception):
    """Raised/returned when a conditional wiki write loses an etag race."""


class AgentRunnerMCPClient(ABC):
    """Typed client over the YoloScribe MCP tools the runner needs."""

    # ── wiki content ──────────────────────────────────────────────────────────
    @abstractmethod
    def wiki_read(self, page_path: str) -> tuple[str, str]:
        """Return (content, etag). ("", "") if the page does not exist."""

    @abstractmethod
    def wiki_write(
        self, page_path: str, content: str, reason: str, expected_etag: str = ""
    ) -> bool:
        """Update a page. With expected_etag, optimistic-concurrency: returns False on
        conflict (caller re-reads and retries); True on success. Empty etag writes
        unconditionally.

        `reason` states why the write is happening (YOL-527). It is required
        rather than defaulted because the MCP `wiki_update` tool rejects an empty
        one — a default here would only move the failure to runtime, on the
        HttpMCPClient path, inside an agent run."""

    @abstractmethod
    def wiki_create(self, page_path: str, content: str, reason: str) -> None:
        """Create a new page (used for parent-path stubs / brand-new pages)."""

    @abstractmethod
    def wiki_list_pages(self) -> list[str]:
        """List first-class wiki page paths (excludes .agents/.user/.archive/.skills)."""

    @abstractmethod
    def propose_page_change(self, page_path: str, content: str, agent_name: str) -> None:
        """Stage a confirm-before-write proposal and fire confirm_page_change."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Semantic/keyword search across the site."""

    # ── ingest ────────────────────────────────────────────────────────────────
    @abstractmethod
    def ingest_list_pending(self) -> list[str]:
        """Filenames waiting in .user/ingest/ (excludes content.md/processed/.agents)."""

    @abstractmethod
    def ingest_read(self, filename: str) -> str | None:
        """Read a pending text file, or None if missing."""

    @abstractmethod
    def ingest_read_bytes(self, filename: str) -> bytes | None:
        """Read a pending binary file's bytes, or None if missing."""

    @abstractmethod
    def ingest_mark_processed(self, filename: str) -> None:
        """Move a pending file to .user/ingest/processed/."""

    @abstractmethod
    def ingest_write_extracted(self, filename: str, markdown: str) -> None:
        """Persist extracted markdown for an ingested binary document."""

    @abstractmethod
    def ingest_read_owner_instructions(self) -> str:
        """The owner-editable routing instructions at .user/ingest/content.md."""

    # ── notifications ─────────────────────────────────────────────────────────
    @abstractmethod
    def notify(self, event_type: str, payload: dict) -> None:
        """Append a notification / decision-signal entry and dispatch on_notify agents."""

    # ── run log ───────────────────────────────────────────────────────────────
    @abstractmethod
    def run_log_append(
        self,
        agent_name: str,
        page_path: str,
        status: str,
        trigger: str,
        duration_s: float,
        detail: str = "",
    ) -> None:
        """Prepend a run entry to the agent's run_log.md (best-effort; never raises)."""


# ── HttpMCPClient — the real transport ────────────────────────────────────────

class HttpMCPClient(AgentRunnerMCPClient):
    """Calls YoloScribe's MCP over streamable-HTTP with the run token.

    Wrap in a ``with`` block (or pass an already-started Strands ``MCPClient``);
    the session must be active for the duration of a run.
    """

    def __init__(self, mcp_url: str, run_token: str) -> None:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient

        headers = {"Authorization": f"Bearer {run_token}"}
        self._client = MCPClient(lambda: streamablehttp_client(mcp_url, headers=headers))
        self._call_seq = 0

    def __enter__(self) -> "HttpMCPClient":
        self._client.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.__exit__(*exc)

    def _call(self, name: str, arguments: dict | None = None) -> Any:
        """Invoke a named tool and return its parsed result (dict / str)."""
        self._call_seq += 1
        result = self._client.call_tool_sync(f"runner-{self._call_seq}", name, arguments or {})
        return _parse_tool_result(name, result)

    # wiki
    def wiki_read(self, page_path: str) -> tuple[str, str]:
        try:
            r = self._call("wiki_read", {"page_path": page_path})
        except Exception:
            return "", ""
        if not isinstance(r, dict):
            return "", ""
        return str(r.get("content", "")), str(r.get("etag", ""))

    def wiki_write(
        self, page_path: str, content: str, reason: str, expected_etag: str = ""
    ) -> bool:
        args = {"page_path": page_path, "content": content, "reason": reason}
        if expected_etag:
            args["expected_etag"] = expected_etag
        r = self._call("wiki_update", args)
        return not (isinstance(r, dict) and r.get("conflict"))

    def wiki_create(self, page_path: str, content: str, reason: str) -> None:
        self._call(
            "wiki_create",
            {"page_path": page_path, "content": content, "reason": reason},
        )

    def wiki_list_pages(self) -> list[str]:
        r = self._call("wiki_list", {})
        pages = r.get("pages", r) if isinstance(r, dict) else r
        return [str(p) for p in pages] if isinstance(pages, list) else []

    def propose_page_change(self, page_path: str, content: str, agent_name: str) -> None:
        self._call("propose_page_change", {
            "page_path": page_path, "content": content, "agent_name": agent_name,
        })

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        r = self._call("search", {"query": query, "limit": limit})
        rows = r.get("results", []) if isinstance(r, dict) else []
        return [
            SearchHit(str(x.get("page_path", "")), float(x.get("score", 0.0)), str(x.get("excerpt", "")))
            for x in rows if isinstance(x, dict)
        ]

    # ingest
    def ingest_list_pending(self) -> list[str]:
        r = self._call("ingest_list_pending", {})
        files = r.get("files", r) if isinstance(r, dict) else r
        return [str(f) for f in files] if isinstance(files, list) else []

    def ingest_read(self, filename: str) -> str | None:
        r = self._call("ingest_read_pending", {"filename": filename})
        if isinstance(r, dict):
            return None if r.get("not_found") else str(r.get("content", ""))
        return None

    def ingest_read_bytes(self, filename: str) -> bytes | None:
        import base64
        r = self._call("ingest_read_pending_bytes", {"filename": filename})
        if isinstance(r, dict) and "content_b64" in r:
            return base64.b64decode(r["content_b64"])
        return None

    def ingest_mark_processed(self, filename: str) -> None:
        self._call("ingest_mark_processed", {"filename": filename})

    def ingest_write_extracted(self, filename: str, markdown: str) -> None:
        self._call("ingest_write_extracted", {"filename": filename, "extracted_markdown": markdown})

    def ingest_read_owner_instructions(self) -> str:
        r = self._call("ingest_read_owner_instructions", {})
        return str(r.get("content", "")) if isinstance(r, dict) else ""

    # notifications
    def notify(self, event_type: str, payload: dict) -> None:
        self._call("notify", {"event_type": event_type, "payload": payload})

    # run log
    def run_log_append(
        self, agent_name: str, page_path: str, status: str,
        trigger: str, duration_s: float, detail: str = "",
    ) -> None:
        try:
            self._call("run_log_append", {
                "agent_name": agent_name, "page_path": page_path, "status": status,
                "trigger": trigger, "duration_s": duration_s, "detail": detail,
            })
        except Exception as exc:
            log.warning("run_log_append failed for %s: %s", agent_name, exc)


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("text", "") or ""
    return getattr(block, "text", "") or ""


def _parse_tool_result(name: str, result: Any) -> Any:
    """Extract a tool's return value (dict/str) from a Strands MCPToolResult.

    Strands delivers the result as a TypedDict:
    ``{status, toolUseId, content, structuredContent, isError}`` — FastMCP puts
    the real return in ``structuredContent`` (a bare value is wrapped as
    ``{"result": ...}``) and mirrors it as a JSON text block in ``content``.
    Prefer structuredContent; fall back to json-decoding the text; else raw text.
    A dict result and the object-attribute form are both handled defensively.
    """
    if isinstance(result, dict):
        status, is_error = result.get("status"), result.get("isError")
        structured, blocks = result.get("structuredContent"), result.get("content") or []
    else:
        status, is_error = getattr(result, "status", "success"), getattr(result, "isError", False)
        structured, blocks = getattr(result, "structuredContent", None), getattr(result, "content", []) or []

    if is_error or status == "error":
        msg = " ".join(_block_text(b) for b in blocks).strip()
        raise RuntimeError(f"MCP tool {name!r} failed: {msg or 'unknown error'}")

    if isinstance(structured, dict):
        return structured.get("result", structured)  # unwrap a wrapped bare return

    text = "".join(_block_text(b) for b in blocks).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


# ── FakeMCPClient — in-memory double for tests / the conformance harness ───────

class FakeMCPClient(AgentRunnerMCPClient):
    """In-memory ``AgentRunnerMCPClient`` with real etag/conflict simulation.

    Pages carry a monotonic etag so ``PageAgent``'s optimistic-concurrency retry
    loop can be exercised. Notifications, proposals, and ingest state are
    inspectable attributes.
    """

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        ingest_files: dict[str, bytes] | None = None,
        owner_instructions: str = "",
        search_results: list[SearchHit] | None = None,
    ) -> None:
        self._pages: dict[str, tuple[str, str]] = {}
        self._etag_seq = 0
        for path, content in (pages or {}).items():
            self._pages[path] = (content, self._next_etag())
        self._ingest: dict[str, bytes] = dict(ingest_files or {})
        self._processed: dict[str, bytes] = {}
        self._extracted: dict[str, str] = {}
        self._owner_instructions = owner_instructions
        self._search_results = list(search_results or [])
        self.notifications: list[tuple[str, dict]] = []
        self.proposals: dict[str, tuple[str, str]] = {}  # page_path -> (content, agent_name)
        self.run_logs: list[dict] = []
        # (page_path, reason) per write, in order — lets tests assert the
        # YOL-527 distillate actually reaches the client, not just that a
        # write happened.
        self.reasons: list[tuple[str, str]] = []

    def _next_etag(self) -> str:
        self._etag_seq += 1
        return f"etag-{self._etag_seq}"

    # wiki
    def wiki_read(self, page_path: str) -> tuple[str, str]:
        return self._pages.get(page_path, ("", ""))

    def wiki_write(
        self, page_path: str, content: str, reason: str, expected_etag: str = ""
    ) -> bool:
        if expected_etag:
            current = self._pages.get(page_path, ("", ""))[1]
            if current != expected_etag:
                return False
        self._pages[page_path] = (content, self._next_etag())
        self.reasons.append((page_path, reason))
        return True

    def wiki_create(self, page_path: str, content: str, reason: str) -> None:
        self._pages[page_path] = (content, self._next_etag())
        self.reasons.append((page_path, reason))

    def wiki_list_pages(self) -> list[str]:
        return sorted(self._pages)

    def propose_page_change(self, page_path: str, content: str, agent_name: str) -> None:
        self.proposals[page_path] = (content, agent_name)
        self.notifications.append(("confirm_page_change", {"page_path": page_path, "agent": agent_name}))

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return self._search_results[:limit]

    # ingest
    def ingest_list_pending(self) -> list[str]:
        return sorted(self._ingest)

    def ingest_read(self, filename: str) -> str | None:
        b = self._ingest.get(filename)
        return b.decode("utf-8") if b is not None else None

    def ingest_read_bytes(self, filename: str) -> bytes | None:
        return self._ingest.get(filename)

    def ingest_mark_processed(self, filename: str) -> None:
        if filename in self._ingest:
            self._processed[filename] = self._ingest.pop(filename)

    def ingest_write_extracted(self, filename: str, markdown: str) -> None:
        self._extracted[filename] = markdown

    def ingest_read_owner_instructions(self) -> str:
        return self._owner_instructions

    # notifications
    def notify(self, event_type: str, payload: dict) -> None:
        self.notifications.append((event_type, dict(payload)))

    # run log
    def run_log_append(
        self, agent_name: str, page_path: str, status: str,
        trigger: str, duration_s: float, detail: str = "",
    ) -> None:
        self.run_logs.append({
            "agent_name": agent_name, "page_path": page_path, "status": status,
            "trigger": trigger, "duration_s": duration_s, "detail": detail,
        })


# ── StorageMCPClient — legacy direct-S3 adapter (strangler-fig; P1.6) ──────────

_INGEST_PREFIX = ".user/ingest/"
_PROCESSED_PREFIX = ".user/ingest/processed/"
_SYSTEM_SEGMENTS = (".agents/", ".user/", ".archive/", ".skills/")


class StorageMCPClient(AgentRunnerMCPClient):
    """AgentRunnerMCPClient implemented over the direct-S3 ``storage`` backend.

    The legacy adapter that keeps the runner working during the strangler-fig
    migration: agents talk only to the ``AgentRunnerMCPClient`` interface, and
    the runner selects this (default, ``AGENT_RUNNER_ACCESS=s3``) or
    ``HttpMCPClient`` (``=mcp``) per the P1.6 flag. It centralizes the IO the
    agent classes used to do inline and is deleted once all sites are on ``mcp``.
    """

    def __init__(self, storage, site: str, search, notify_fn, user_id: str = "") -> None:
        self._storage = storage
        self._site = site
        self._search = search
        self._notify_fn = notify_fn
        self._user_id = user_id

    def _wiki(self, page_path: str):
        from yoloscribe_io import WikiPageMarkdownFile
        return WikiPageMarkdownFile(site=self._site, page_path=page_path, storage=self._storage)

    # wiki
    def wiki_read(self, page_path: str) -> tuple[str, str]:
        content, etag = self._wiki(page_path).read_with_etag()
        return (content or "", etag or "")

    def wiki_write(
        self, page_path: str, content: str, reason: str, expected_etag: str = ""
    ) -> bool:
        wiki = self._wiki(page_path)
        if expected_etag:
            return wiki.write_conditional(
                content, expected_etag, user_id=self._user_id, reason=reason
            )
        wiki.write(content, user_id=self._user_id, reason=reason)
        return True

    def wiki_create(self, page_path: str, content: str, reason: str) -> None:
        self._wiki(page_path).write(content, user_id=self._user_id, reason=reason)

    def wiki_list_pages(self) -> list[str]:
        prefix = f"{self._site}/"
        pages = []
        for key in self._storage.list(prefix):
            if not key.endswith("/content.md"):
                continue
            rel = key[len(prefix):]
            if any(seg in f"/{rel}" for seg in _SYSTEM_SEGMENTS):
                continue
            pages.append(rel[: -len("/content.md")] if rel != "content.md" else "")
        return sorted(pages)

    def propose_page_change(self, page_path: str, content: str, agent_name: str) -> None:
        import json as _json
        base = f"{self._site}/{page_path}/" if page_path else f"{self._site}/"
        proposed_key = f"{base}.proposed.content.md"
        meta_key = f"{base}.proposed.content.meta.json"
        agent_md_key = f"{base}.agents/{agent_name}/agent.md"
        self._storage.write(proposed_key, content)
        self._storage.write(meta_key, _json.dumps({"agent_md_key": agent_md_key}))
        self._notify_fn(
            "confirm_page_change",
            {"agent": agent_name, "content_key": f"{base}content.md", "proposed_key": proposed_key},
            self._user_id,
        )

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        results = self._search.search(query, self._site, limit=limit)
        return [SearchHit(r.page_path, r.score, r.excerpt) for r in results]

    # ingest
    def ingest_list_pending(self) -> list[str]:
        prefix = f"{self._site}/{_INGEST_PREFIX}"
        processed_prefix = f"{self._site}/{_PROCESSED_PREFIX}"
        pending = []
        for key in self._storage.list(prefix):
            rel = key[len(prefix):]
            if not rel or rel == "content.md" or key.startswith(processed_prefix):
                continue
            if "/.agents/" in rel or rel.startswith(".agents/"):
                continue
            pending.append(rel)
        return sorted(pending)

    def ingest_read(self, filename: str) -> str | None:
        return self._storage.read(f"{self._site}/{_INGEST_PREFIX}{filename.lstrip('/')}")

    def ingest_read_bytes(self, filename: str) -> bytes | None:
        return self._storage.read_bytes(f"{self._site}/{_INGEST_PREFIX}{filename.lstrip('/')}")

    def ingest_mark_processed(self, filename: str) -> None:
        filename = filename.lstrip("/")
        self._storage.move(
            f"{self._site}/{_INGEST_PREFIX}{filename}",
            f"{self._site}/{_PROCESSED_PREFIX}{filename}",
        )

    def ingest_write_extracted(self, filename: str, markdown: str) -> None:
        key = f"{self._site}/{_INGEST_PREFIX}{filename.lstrip('/')}.extracted.md"
        self._storage.write_bytes(key, markdown.encode("utf-8"), "text/markdown")

    def ingest_read_owner_instructions(self) -> str:
        return (self._storage.read(f"{self._site}/{_INGEST_PREFIX}content.md") or "").strip()

    # notifications
    def notify(self, event_type: str, payload: dict) -> None:
        self._notify_fn(event_type, payload, self._user_id)

    # run log
    def run_log_append(
        self, agent_name: str, page_path: str, status: str,
        trigger: str, duration_s: float, detail: str = "",
    ) -> None:
        import datetime
        base = (
            f"{self._site}/{page_path}/.agents/{agent_name}"
            if page_path else f"{self._site}/.agents/{agent_name}"
        )
        run_log_key = f"{base}/run_log.md"
        now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"## {agent_name} — {now}", "",
            f"**Status:** {status}  ",
            f"**Trigger:** {trigger}  ",
            f"**Duration:** {duration_s:.1f}s",
        ]
        if detail:
            lines += ["", detail]
        lines += ["", "---", ""]
        entry = "\n".join(lines) + "\n"
        try:
            existing = self._storage.read(run_log_key) or ""
            self._storage.write(run_log_key, entry + existing)
        except Exception as exc:
            log.warning("Failed to write run_log %s: %s", run_log_key, exc)
