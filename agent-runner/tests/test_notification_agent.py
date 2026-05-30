"""Tests for NotificationAgent (YOL-297)."""
from __future__ import annotations

from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.notification import NotificationAgent
from agent_runner.agents.search import NullSearchBackend
from tests.conftest import make_def, make_notify


def _make_agent(storage: LocalStorageBackend, **def_kwargs) -> NotificationAgent:
    return NotificationAgent(
        agent_def=make_def(trigger="on_notify", events=["page_shared"], **def_kwargs),
        site="s",
        page_path="",
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="u1",
        notify_fn=make_notify(),
        search=NullSearchBackend(),
    )


# ── construction ──────────────────────────────────────────────────────────────

def test_notification_agent_is_constructable():
    agent = _make_agent(LocalStorageBackend())
    assert agent.agent_def.trigger == "on_notify"


# ── system prompt ─────────────────────────────────────────────────────────────

def test_system_prompt_includes_description():
    agent = _make_agent(LocalStorageBackend(), description="Post to Slack.")
    prompt = agent._build_system_prompt()
    assert "Post to Slack." in prompt


def test_system_prompt_includes_dispatch_instruction():
    agent = _make_agent(LocalStorageBackend())
    prompt = agent._build_system_prompt()
    assert "dispatch" in prompt.lower()


# ── no wiki tools ─────────────────────────────────────────────────────────────

def test_notification_agent_has_no_wiki_read_tool():
    agent = _make_agent(LocalStorageBackend())
    # NotificationAgent exposes no page_read / wiki_read methods
    assert not hasattr(agent, "page_read")
    assert not hasattr(agent, "wiki_read")


def test_notification_agent_has_no_wiki_write_tool():
    agent = _make_agent(LocalStorageBackend())
    assert not hasattr(agent, "page_write")
    assert not hasattr(agent, "wiki_write")


# ── mcp_tools passed through ─────────────────────────────────────────────────

def test_mcp_tools_stored():
    fake_tool = object()
    agent = NotificationAgent(
        agent_def=make_def(trigger="on_notify", events=["x"]),
        site="s",
        page_path="",
        storage=LocalStorageBackend(),
        mcp_tools=[fake_tool],
        model=None,
        user_id="u1",
        notify_fn=make_notify(),
    )
    assert fake_tool in agent._mcp_tools
