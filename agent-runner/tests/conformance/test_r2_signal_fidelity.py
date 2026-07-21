"""R2 — Signal fidelity.

Contract §5.2 / §8: after a run that routes content, structures a page,
suppresses a notification, and rejects a proposal, the Yolo Brain signal log
MUST contain exactly the expected typed signals — including the two no-write
decision signals (`notification_suppressed`, `proposal_rejected`).

Tier A (in-process). This is a checklist, not a binary pass/fail (per the
re-architecture plan's own "R2 partial" framing) — it drives a real Ingest +
Page cycle through the actual agent-runner classes and reports, per expected
signal type, whether it landed in `.user/librarian/signal-log.md`
(`SignalLog`, yoloscribe_io/librarian.py). `notification_suppressed` and
`proposal_rejected`/`proposal_accepted` have no code path anywhere in
agent-runner today — they only appear as instructions inside the interactive
Librarian *chat* agent's system prompt (backend/agents/librarian.py), an
unrelated surface — so this checklist reports them absent with that context
rather than attempting to synthesize a scenario that doesn't exist in code.
"""
from __future__ import annotations

import pytest
from agent_runner.agents.ingest import IngestAgent
from agent_runner.agents.page import PageAgent
from yoloscribe_io import AgentDefinition, SignalLog, WikiPageMarkdownFile
from yoloscribe_io.storage import LocalStorageBackend

from .support.report import REPORT, RResult
from .support.scripted_model import ScriptedModel, ToolCall

# Expected typed signals per the Runtime Conformance Contract §5.2 and the KM
# Signal Spec. `agent_run_success`/`agent_run_failure` are real signal types
# too, but they're written by agent_runner.main()'s top-level wrapper (Tier
# B), not by the agent classes exercised here — see the detail note below.
_EXPECTED_SIGNAL_TYPES = [
    "content_routed",
    "page_structured",
    "page_enriched",
    "agent_provisioned",
    "notification_suppressed",
    "proposal_accepted",
    "proposal_rejected",
    "user_instruction",
    "ingest_unrouted",
    "ingest_start",
    "ingest_end",
]

# These exist today as *notifications* (.user/notifications.md), not signals.
_NOTIFICATION_ONLY_TYPES = {"ingest_start", "ingest_end", "ingest_unrouted"}


@pytest.mark.conformance
def test_r2_signal_fidelity():
    storage = LocalStorageBackend()
    site = "conformance-r2"
    notifications: list[tuple[str, dict, str]] = []

    def notify_fn(event_type: str, payload: dict, user_id: str = "") -> None:
        notifications.append((event_type, payload, user_id))

    # ── Ingest scenario: route a note to a new page ────────────────────────
    storage.write(f"{site}/.user/ingest/note.md", "Meeting notes about the roadmap.")

    ingest_def = AgentDefinition(name="ingester", trigger="schedule", type="ingest")
    ingest_agent = IngestAgent(
        agent_def=ingest_def,
        site=site,
        page_path=".user/ingest",
        storage=storage,
        mcp_tools=[],
        model=ScriptedModel(
            [
                [ToolCall("ingest_list_pending", {})],
                [ToolCall("ingest_read", {"filename": "note.md"})],
                [ToolCall("wiki_list_pages", {})],
                [ToolCall("wiki_write", {"page_path": "notes", "content": "# Notes\n\nMeeting notes about the roadmap.\n"})],
                [ToolCall("ingest_mark_processed", {"filename": "note.md"})],
                [ToolCall("ingest_complete", {"summary": "Routed note.md to notes."})],
                "Ingest run complete.",
            ]
        ),
        user_id="test-user",
        notify_fn=notify_fn,
    )
    ingest_agent.run("Process the ingest queue.")

    # ── Page scenario: structure an existing page ──────────────────────────
    page_content_key = f"{site}/notes/content.md"
    page_def = AgentDefinition(name="structurer", trigger="on_write", type="page")
    wiki = WikiPageMarkdownFile(site=site, page_path="notes", storage=storage)
    page_agent = PageAgent(
        agent_def=page_def,
        site=site,
        page_path="notes",
        wiki=wiki,
        storage=storage,
        mcp_tools=[],
        model=ScriptedModel(["# Notes\n\n## Roadmap\n\nMeeting notes about the roadmap.\n"]),
        user_id="test-user",
        notify_fn=notify_fn,
        content_key=page_content_key,
        agent_md_key=f"{site}/notes/.agents/structurer/agent.md",
    )
    page_agent.run("Structure this page with headings.")

    # ── Check the signal log ────────────────────────────────────────────────
    signal_log = SignalLog(site=site, storage=storage)
    raw_log = signal_log.read_all()

    checklist = {f"{sig_type} (signal)": f"— {sig_type}" in raw_log for sig_type in _EXPECTED_SIGNAL_TYPES}
    present_count = sum(checklist.values())

    notified_types = {n[0] for n in notifications}
    notification_only_present = sorted(t for t in _NOTIFICATION_ONLY_TYPES if t in notified_types)

    if present_count == len(checklist):
        status = "PASS"
    elif present_count == 0:
        status = "FAIL"
    else:
        status = "PARTIAL"

    detail = (
        f"{present_count}/{len(checklist)} expected signal types found in the signal log. "
        "Today, agent-runner's agent classes never call SignalLog directly — only "
        "agent_runner.main()'s top-level wrapper does, for agent_run_success/"
        "agent_run_failure (not checked here; see R1, which exercises that wrapper). "
    )
    if notification_only_present:
        detail += (
            f"{', '.join(notification_only_present)} fired as notification(s) "
            "(.user/notifications.md) during this run but not as signals — the "
            "distinction P1.3 has to close."
        )

    REPORT.record(
        RResult(
            id="R2",
            name="Signal fidelity",
            status=status,
            detail=detail,
            checklist=checklist,
        )
    )
    # Not a hard pytest failure — R2 is intentionally allowed to be red today;
    # the assertion just guards against the checklist itself being broken.
    assert isinstance(checklist, dict) and checklist
