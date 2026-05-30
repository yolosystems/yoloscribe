"""Shared fixtures for agent-runner unit tests."""
from __future__ import annotations

import pytest

from yoloscribe_io import AgentDefinition, Scope
from yoloscribe_io.storage import LocalStorageBackend


def make_def(**kwargs) -> AgentDefinition:
    defaults = dict(name="test-agent", trigger="on_write")
    defaults.update(kwargs)
    return AgentDefinition(**defaults)


def make_notify():
    """Return a notify callable that records its calls."""
    calls: list[tuple] = []

    def notify(event_type: str, payload: dict, user_id: str = "") -> None:
        calls.append((event_type, payload, user_id))

    notify.calls = calls  # type: ignore[attr-defined]
    return notify


@pytest.fixture
def storage():
    return LocalStorageBackend()


@pytest.fixture
def notify_fn():
    return make_notify()
