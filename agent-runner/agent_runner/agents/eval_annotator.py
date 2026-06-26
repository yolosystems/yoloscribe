"""EvalAnnotatorAgent — reads annotation logs and submits feedback to Phoenix."""

from __future__ import annotations

import logging
import re
from typing import Callable

from strands_tools import http_request
from yoloscribe_io import AgentDefinition

from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)

# Matches an annotation field line: **Field:** value (with optional comment stripping)
_ANNOTATION_RE = re.compile(
    r"^\*\*(?P<key>Rating|Notes|Correction):\*\*\s*(?P<value>.*?)(\s*<!--.*-->)?\s*$",
    re.IGNORECASE,
)

# A field is blank if it's empty, or is just an HTML comment template
_BLANK_RE = re.compile(r"^\s*(<!--.*?-->)?\s*$")


def _parse_annotations(content: str) -> dict[str, str]:
    """Extract Rating/Notes/Correction values from the annotation log body.

    Returns a dict with keys 'rating', 'notes', 'correction'. A field is
    considered blank (and omitted) if it contains only whitespace or an HTML
    comment template (the placeholder left by the trace fetcher).
    """
    result: dict[str, str] = {}
    for line in content.splitlines():
        m = _ANNOTATION_RE.match(line)
        if not m:
            continue
        key = m.group("key").lower()
        value = m.group("value").strip()
        if not _BLANK_RE.match(value):
            result[key] = value
    return result


def _parse_session_id(content: str) -> str:
    """Extract session_id from YAML frontmatter."""
    m = re.search(r"^session_id:\s*(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else ""


class EvalAnnotatorAgent(BaseAgent):
    """Platform-provisioned agent that submits run log annotations to Phoenix.

    This agent is automatically provisioned when a user creates an agent with
    eval_log: true. It is dispatched by type: eval_annotator and is NOT
    user-configurable. The annotate_trace tool on the MCP server enforces
    site-scoped access before writing to Phoenix.

    Inputs (via CONTENT_KEY → run log .md):
    - Parsed annotation fields: Rating, Notes, Correction
    - Session ID from YAML frontmatter

    The agent no-ops (logs a message and returns 0) if no annotation fields
    are filled in — this is the expected case on initial write by the trace
    fetcher, before the user has added their feedback.
    """

    FIXED_SYSTEM_PROMPT = (
        "You are the YoloScribe eval annotator. Your task is to read an agent run log "
        "and submit any human feedback annotations to Phoenix for evaluation purposes.\n\n"
        "The run log is provided as the user message. It contains YAML frontmatter with "
        "a session_id, a conversation trace, and annotation fields (Rating, Notes, Correction).\n\n"
        "Use the annotate_trace tool to submit the annotations. Only call the tool if the "
        "Rating field is filled in (not blank, not a comment placeholder). If there are no "
        "annotations, output 'No annotations to submit.' and stop.\n\n"
        "Do not modify the run log. Do not read or write any wiki pages. "
        "Do not use any tools other than annotate_trace."
    )

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
        content_key: str = "",
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
        self._run_log_key = content_key

    def _build_system_prompt(self) -> str:
        return self.FIXED_SYSTEM_PROMPT

    def run(self, prompt: str) -> int:
        """Read the run log from storage and submit annotations to Phoenix."""
        run_log_content = self._storage.read(self._run_log_key) if self._run_log_key else ""
        if not run_log_content:
            log.warning("EvalAnnotatorAgent: run log not found at %s — aborting", self._run_log_key)
            return 0

        annotations = _parse_annotations(run_log_content)
        if not annotations.get("rating"):
            log.info(
                "EvalAnnotatorAgent: no rating found in run log — skipping annotation"
            )
            return 0

        session_id = _parse_session_id(run_log_content)
        if not session_id:
            log.warning("EvalAnnotatorAgent: no session_id in run log frontmatter — aborting")
            return 0

        log.info(
            "EvalAnnotatorAgent: submitting annotation for session %s (rating=%s)",
            session_id[:8], annotations["rating"],
        )

        # Use only the annotate_trace tool from the platform MCP server.
        # The MCP tools list is injected by the dispatcher; it includes the
        # yoloscribe platform MCP client which exposes annotate_trace.
        agent = self._make_strands_agent(self._mcp_tools)

        task = (
            f"Submit annotations for this run log.\n\n"
            f"session_id: {session_id}\n"
            f"rating: {annotations['rating']}\n"
            f"notes: {annotations.get('notes', '')}\n"
            f"correction: {annotations.get('correction', '')}\n\n"
            f"Call annotate_trace with the above values now."
        )
        result = agent(task)

        # YOL-410: write eval_annotation signal and run memory reasoner inline.
        try:
            import os
            import yaml
            from yoloscribe_io import SignalEntry, SignalLog, MemoryFile, conclusion_to_dict
            from yoloscribe_io.librarian import _conclusion_from_dict
            from ..memory_reasoner import HaikuMemoryReasoner

            agent_md_key = ""
            if self._run_log_key and "/runs/" in self._run_log_key:
                agent_md_key = self._run_log_key.split("/runs/")[0] + "/agent.md"

            eval_payload = {
                "agent_md_key": agent_md_key,
                "page_path": self._page_path,
                "rating": annotations["rating"],
                "notes": annotations.get("notes", ""),
                "correction": annotations.get("correction", ""),
            }
            sl = SignalLog(site=self._site, storage=self._storage)
            sl.append(SignalEntry(type="eval_annotation", payload=eval_payload))

            if os.environ.get("LIBRARIAN_MEMORY_ENABLED", "true").lower() in ("1", "true", "yes"):
                mf = MemoryFile(site=self._site, storage=self._storage)
                _, existing = mf.read()
                existing_yaml = (
                    yaml.dump(
                        [conclusion_to_dict(c) for c in existing],
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                    if existing
                    else ""
                )
                raw = HaikuMemoryReasoner().derive("eval_annotation", eval_payload, existing_yaml)
                if raw:
                    new_conclusions = [
                        _conclusion_from_dict(d) for d in raw if isinstance(d, dict)
                    ]
                    mf.upsert(new_conclusions)
        except Exception as exc:
            log.warning("EvalAnnotatorAgent: eval_annotation signal write failed: %s", exc)

        return result.metrics.accumulated_usage.get("totalTokens", 0)
