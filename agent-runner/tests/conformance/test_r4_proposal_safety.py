"""R4 — Proposal safety.

Contract §5.7 / §8: with `confirm_before_write: true`, a write MUST be staged
to `.proposed.content.md` and a `confirm_page_change` notification emitted —
the live page MUST NOT be mutated.

Tier A (in-process, LocalStorageBackend) — this is self-contained logic in
`PageAgent._run_propose_mode` (page.py) that doesn't depend on anything the
re-architecture is changing, so it's the one criterion expected to already
pass today.
"""
from __future__ import annotations

import pytest
from agent_runner.agents.page import PageAgent
from yoloscribe_io import AgentDefinition, WikiPageMarkdownFile
from yoloscribe_io.storage import LocalStorageBackend

from .support.report import REPORT, RResult
from .support.scripted_model import ScriptedModel

_ORIGINAL = "# Original\n\nOriginal content.\n"
_PROPOSED = "# Updated\n\nProposed new content.\n"


@pytest.mark.conformance
def test_r4_proposal_safety():
    storage = LocalStorageBackend()
    site = "conformance-r4"
    content_key = f"{site}/content.md"
    agent_md_key = f"{site}/.agents/proposer/agent.md"
    storage.write(content_key, _ORIGINAL)

    notifications: list[tuple[str, dict, str]] = []

    def notify_fn(event_type: str, payload: dict, user_id: str = "") -> None:
        notifications.append((event_type, payload, user_id))

    agent_def = AgentDefinition(
        name="proposer",
        trigger="on_write",
        type="page",
        description="Propose a rewrite.",
        confirm_before_write=True,
    )
    wiki = WikiPageMarkdownFile(site=site, page_path="", storage=storage)
    model = ScriptedModel([_PROPOSED])

    agent = PageAgent(
        agent_def=agent_def,
        site=site,
        page_path="",
        wiki=wiki,
        storage=storage,
        mcp_tools=[],
        model=model,
        user_id="test-user",
        notify_fn=notify_fn,
        content_key=content_key,
        agent_md_key=agent_md_key,
    )
    agent.run("Rewrite the page.")

    proposed_key = f"{site}/.proposed.content.md"
    meta_key = f"{site}/.proposed.content.meta.json"
    got_confirm_notification = any(n[0] == "confirm_page_change" for n in notifications)

    checklist = {
        "live content.md unchanged": storage.read(content_key) == _ORIGINAL,
        ".proposed.content.md written with new content": storage.read(proposed_key) == _PROPOSED,
        ".proposed.content.meta.json written": storage.read(meta_key) is not None,
        "confirm_page_change notification fired": got_confirm_notification,
    }
    passed = all(checklist.values())
    REPORT.record(
        RResult(
            id="R4",
            name="Proposal safety",
            status="PASS" if passed else "FAIL",
            detail=(
                "confirm_before_write stages .proposed.content.md and fires "
                "confirm_page_change without touching the live page."
                if passed
                else "one or more proposal-safety guarantees did not hold — see checklist."
            ),
            checklist=checklist,
        )
    )
    assert passed, checklist
