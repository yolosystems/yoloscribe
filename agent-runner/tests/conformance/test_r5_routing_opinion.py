"""R5 — Routing opinion.

Contract §6.2 / §8: an Ingest Agent MUST route a file per the owner's
routing-instructions file (`.user/ingest/content.md`) even when the model's
own judgement would differ, and honor wiki structure for the rest.

Two tiers:

- **Plumbing (Tier A, default, always runs).** Proves the *mechanism* is
  wired: `IngestAgent._read_owner_instructions()` reads
  `.user/ingest/content.md` and `_build_system_prompt()` embeds it with
  "priority over your own judgement" framing. This does NOT prove the
  contract's actual claim (that the model obeys it over its own judgement) —
  that's a claim about model behavior, not plumbing.
- **Model judgment (Tier B, opt-in via `CONFORMANCE_LIVE_LLM=1`, requires a
  real `ANTHROPIC_API_KEY`).** Seeds a scratch wiki where the topically
  obvious destination for a file conflicts with the owner's explicit routing
  override, runs a real IngestAgent against a live Haiku model, and asserts
  the file lands where the owner's instructions say — not where topical
  similarity alone would send it. Off by default: costs real API spend and
  can be flaky. This is the one criterion a ScriptedModel can't test — a
  scripted script would be circular (it can only prove what it was told to
  prove), so this is the sole place the harness makes a real model call.
"""
from __future__ import annotations

import os

import pytest
from agent_runner.agents.ingest import IngestAgent
from yoloscribe_io import AgentDefinition
from yoloscribe_io.storage import LocalStorageBackend

from .support.report import REPORT, RResult

_OWNER_INSTRUCTIONS = "Route all meeting notes under meetings/, never under notes/."

# Live-judgment scenario: "engineering-notes" is the topically obvious match
# (it's about the exact same subject), but the owner's instructions redirect
# anything about roadmap meetings to "meetings/" instead.
_LIVE_OWNER_INSTRUCTIONS = (
    "Anything about roadmap meetings must be routed under meetings/, even if "
    "engineering-notes/ looks like a closer topical match. This overrides your "
    "own judgement — always prefer meetings/ for meeting content."
)
_LIVE_INGEST_CONTENT = (
    "Notes from today's engineering roadmap sync: decided to prioritize the "
    "search reindex work next quarter."
)


@pytest.mark.conformance
def test_r5_routing_opinion():
    storage = LocalStorageBackend()
    site = "conformance-r5"
    storage.write(f"{site}/.user/ingest/content.md", _OWNER_INSTRUCTIONS)

    agent_def = AgentDefinition(name="ingester", trigger="schedule", type="ingest")
    agent = IngestAgent(
        agent_def=agent_def,
        site=site,
        page_path=".user/ingest",
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="test-user",
        notify_fn=lambda *a, **k: None,
    )

    loaded = agent._read_owner_instructions()
    agent._owner_instructions = loaded
    system_prompt = agent._build_system_prompt()

    checklist = {
        "owner instructions loaded from .user/ingest/content.md": loaded == _OWNER_INSTRUCTIONS,
        "instructions text present in system prompt": _OWNER_INSTRUCTIONS in system_prompt,
        "priority-over-judgement framing present": "priority over your own judgement" in system_prompt,
    }
    plumbing_passed = all(checklist.values())

    live_result: bool | None = None
    if os.environ.get("CONFORMANCE_LIVE_LLM") == "1":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("CONFORMANCE_LIVE_LLM=1 set but ANTHROPIC_API_KEY is missing")
        live_result = _run_live_judgment_check()
        checklist["model-judgment: routes per owner override, not topical similarity (opt-in, live Haiku)"] = (
            live_result
        )

    if live_result is None:
        status = "PARTIAL" if plumbing_passed else "FAIL"
        detail = (
            f"plumbing: {'PASS' if plumbing_passed else 'FAIL'} (instructions file loaded and "
            "injected with priority framing). model-judgment: UNTESTED — opt in with "
            "CONFORMANCE_LIVE_LLM=1 (costs real Anthropic API spend, non-deterministic)."
        )
    else:
        status = "PASS" if (plumbing_passed and live_result) else "FAIL"
        detail = (
            f"plumbing: {'PASS' if plumbing_passed else 'FAIL'}. "
            f"model-judgment (live Haiku): {'PASS' if live_result else 'FAIL'}."
        )

    REPORT.record(RResult(id="R5", name="Routing opinion", status=status, detail=detail, checklist=checklist))
    assert plumbing_passed, checklist


def _run_live_judgment_check() -> bool:
    """Opt-in only. Makes a real Anthropic API call — see module docstring."""
    from strands.models.anthropic import AnthropicModel

    storage = LocalStorageBackend()
    site = "conformance-r5-live"
    storage.write(f"{site}/.user/ingest/content.md", _LIVE_OWNER_INSTRUCTIONS)
    storage.write(f"{site}/.user/ingest/sync-notes.md", _LIVE_INGEST_CONTENT)
    storage.write(f"{site}/engineering-notes/content.md", "# Engineering Notes\n")
    storage.write(f"{site}/meetings/content.md", "# Meetings\n")

    agent_def = AgentDefinition(name="ingester", trigger="schedule", type="ingest")
    model = AnthropicModel(model_id="claude-haiku-4-5-20251001", max_tokens=4096, client_args={"max_retries": 0})
    agent = IngestAgent(
        agent_def=agent_def,
        site=site,
        page_path=".user/ingest",
        storage=storage,
        mcp_tools=[],
        model=model,
        user_id="test-user",
        notify_fn=lambda *a, **k: None,
    )
    agent.run("Process the ingest queue.")

    meetings_content = storage.read(f"{site}/meetings/content.md") or ""
    engineering_content = storage.read(f"{site}/engineering-notes/content.md") or ""
    routed_to_meetings = "roadmap" in meetings_content.lower() or "sync" in meetings_content.lower()
    routed_to_engineering = "roadmap" in engineering_content.lower() or "sync" in engineering_content.lower()
    return routed_to_meetings and not routed_to_engineering
