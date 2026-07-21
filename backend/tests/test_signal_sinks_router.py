"""Unit tests for the /signal-sinks/webhooks CRUD endpoints (YOL-495)."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException


class _Store:
    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value, description=""):
        self._data[key] = value

    def exists(self, key):
        return key in self._data

    def delete(self, key):
        self._data.pop(key, None)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _patch_store(monkeypatch):
    import routers.signal_sinks as mod
    store = _Store()
    monkeypatch.setattr(mod, "secrets_store", store)
    return store


class TestListSignalSinkWebhooks:
    def test_empty_by_default(self):
        import routers.signal_sinks as mod
        result = _run(mod.list_signal_sink_webhooks(site="alice-site", ctx=("u1", "alice-site")))
        assert result == {"webhooks": []}

    def test_non_owner_rejected(self):
        import routers.signal_sinks as mod
        with pytest.raises(HTTPException):
            _run(mod.list_signal_sink_webhooks(site="alice-site", ctx=("u1", "bobs-site")))


class TestAddSignalSinkWebhook:
    def test_adds_and_masks_secret_in_listing(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry

        result = _run(mod.add_signal_sink_webhook(
            SignalSinkWebhookEntry(label="collector", url="https://collect.example.com/x", secret="topsecret"),
            site="alice-site", ctx=("u1", "alice-site"),
        ))
        assert result == {"status": "added", "index": 0}

        listed = _run(mod.list_signal_sink_webhooks(site="alice-site", ctx=("u1", "alice-site")))
        assert listed["webhooks"] == [{"index": 0, "label": "collector", "url": "https://collect.example.com/x", "has_secret": True}]

    def test_rejects_invalid_url(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry

        with pytest.raises(HTTPException) as exc_info:
            _run(mod.add_signal_sink_webhook(
                SignalSinkWebhookEntry(url="not-a-url"),
                site="alice-site", ctx=("u1", "alice-site"),
            ))
        assert exc_info.value.status_code == 400

    def test_rejects_when_not_owner(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry

        with pytest.raises(HTTPException):
            _run(mod.add_signal_sink_webhook(
                SignalSinkWebhookEntry(url="https://collect.example.com/x"),
                site="alice-site", ctx=("u1", "bobs-site"),
            ))

    def test_enforces_max_targets(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry, _MAX_TARGETS

        for i in range(_MAX_TARGETS):
            _run(mod.add_signal_sink_webhook(
                SignalSinkWebhookEntry(url=f"https://collect.example.com/{i}"),
                site="alice-site", ctx=("u1", "alice-site"),
            ))
        with pytest.raises(HTTPException) as exc_info:
            _run(mod.add_signal_sink_webhook(
                SignalSinkWebhookEntry(url="https://collect.example.com/overflow"),
                site="alice-site", ctx=("u1", "alice-site"),
            ))
        assert exc_info.value.status_code == 400


class TestDeleteSignalSinkWebhook:
    def test_deletes_by_index(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry

        _run(mod.add_signal_sink_webhook(
            SignalSinkWebhookEntry(url="https://collect.example.com/x"),
            site="alice-site", ctx=("u1", "alice-site"),
        ))
        result = _run(mod.delete_signal_sink_webhook(0, site="alice-site", ctx=("u1", "alice-site")))
        assert result == {"status": "deleted"}
        listed = _run(mod.list_signal_sink_webhooks(site="alice-site", ctx=("u1", "alice-site")))
        assert listed["webhooks"] == []

    def test_out_of_range_index_404s(self):
        import routers.signal_sinks as mod

        with pytest.raises(HTTPException) as exc_info:
            _run(mod.delete_signal_sink_webhook(0, site="alice-site", ctx=("u1", "alice-site")))
        assert exc_info.value.status_code == 404

    def test_rejects_when_not_owner(self):
        import routers.signal_sinks as mod
        from routers.signal_sinks import SignalSinkWebhookEntry

        _run(mod.add_signal_sink_webhook(
            SignalSinkWebhookEntry(url="https://collect.example.com/x"),
            site="alice-site", ctx=("u1", "alice-site"),
        ))
        with pytest.raises(HTTPException):
            _run(mod.delete_signal_sink_webhook(0, site="alice-site", ctx=("u1", "bobs-site")))
