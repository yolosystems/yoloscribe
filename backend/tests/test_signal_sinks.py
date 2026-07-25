"""Unit tests for the pluggable SignalSink interface (YOL-495)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from signal_sinks import CompositeSignalSink, NullSignalSink, WebhookSignalSink, create_signal_sink
from signal_sinks.webhook import signal_sink_webhooks_key


class _Store:
    """In-memory SecretsStore double."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data = dict(initial or {})

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str, description: str = "") -> None:
        self._data[key] = value

    def exists(self, key: str) -> bool:
        return key in self._data

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class TestNullSignalSink:
    def test_emit_does_nothing(self):
        sink = NullSignalSink()
        sink.emit("some-site", "page_created", {"page_path": "x"})  # must not raise


class TestWebhookSignalSink:
    def test_no_targets_configured_is_a_noop(self, monkeypatch):
        import httpx

        called = []
        monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: called.append(1))
        sink = WebhookSignalSink(_Store())
        sink.emit("alice-site", "page_created", {"page_path": "x"})
        assert called == []

    def test_posts_to_each_configured_target(self, monkeypatch):
        import json
        import httpx

        store = _Store({
            signal_sink_webhooks_key("alice-site"): json.dumps([
                {"label": "collector-1", "url": "https://collect.example.com/a", "secret": "s1"},
                {"label": "collector-2", "url": "https://collect.example.com/b", "secret": ""},
            ])
        })
        posts = []

        class _FakeResp:
            def raise_for_status(self):
                pass

        def fake_post(self, url, json=None, headers=None):
            posts.append((url, json, headers))
            return _FakeResp()

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        sink = WebhookSignalSink(store)
        sink.emit("alice-site", "page_created", {"page_path": "features/x"})

        assert len(posts) == 2
        url1, body1, headers1 = posts[0]
        assert url1 == "https://collect.example.com/a"
        assert body1["signal_type"] == "page_created"
        assert body1["payload"] == {"page_path": "features/x"}
        assert body1["site"] == "alice-site"
        assert "at" in body1
        assert headers1["X-Signal-Secret"] == "s1"

        url2, body2, headers2 = posts[1]
        assert url2 == "https://collect.example.com/b"
        assert "X-Signal-Secret" not in headers2

    def test_corrupt_config_is_treated_as_no_targets(self, monkeypatch):
        import httpx

        called = []
        monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: called.append(1))
        store = _Store({signal_sink_webhooks_key("s"): "not-json"})
        sink = WebhookSignalSink(store)
        sink.emit("s", "page_created", {})
        assert called == []

    def test_one_target_failing_does_not_prevent_others(self, monkeypatch):
        import json
        import httpx

        store = _Store({
            signal_sink_webhooks_key("s"): json.dumps([
                {"label": "bad", "url": "https://bad.example.com", "secret": ""},
                {"label": "good", "url": "https://good.example.com", "secret": ""},
            ])
        })
        posted_urls = []

        def fake_post(self, url, json=None, headers=None):
            if "bad" in url:
                raise httpx.ConnectError("boom")
            posted_urls.append(url)

            class _Resp:
                def raise_for_status(self):
                    pass
            return _Resp()

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        sink = WebhookSignalSink(store)
        sink.emit("s", "page_created", {})  # must not raise
        assert posted_urls == ["https://good.example.com"]


class TestCompositeSignalSink:
    def test_emits_to_every_sink(self):
        calls = []

        class _RecordingSink:
            def __init__(self, name):
                self.name = name

            def emit(self, site, signal_type, payload):
                calls.append((self.name, site, signal_type, payload))

        sink = CompositeSignalSink([_RecordingSink("a"), _RecordingSink("b")])
        sink.emit("s", "page_created", {"k": "v"})
        assert calls == [
            ("a", "s", "page_created", {"k": "v"}),
            ("b", "s", "page_created", {"k": "v"}),
        ]

    def test_one_sink_raising_does_not_block_others(self):
        calls = []

        class _RaisingSink:
            def emit(self, site, signal_type, payload):
                raise RuntimeError("boom")

        class _RecordingSink:
            def emit(self, site, signal_type, payload):
                calls.append((site, signal_type, payload))

        sink = CompositeSignalSink([_RaisingSink(), _RecordingSink()])
        sink.emit("s", "page_created", {})  # must not raise
        assert calls == [("s", "page_created", {})]

    def test_empty_sink_list_is_a_noop(self):
        CompositeSignalSink([]).emit("s", "page_created", {})  # must not raise


class TestCreateSignalSink:
    def test_returns_composite_including_webhook_sink(self):
        sink = create_signal_sink(_Store())
        assert isinstance(sink, CompositeSignalSink)
        assert any(isinstance(s, WebhookSignalSink) for s in sink._sinks)

    def test_created_sink_is_a_noop_with_no_targets_configured(self, monkeypatch):
        import httpx

        called = []
        monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: called.append(1))
        sink = create_signal_sink(_Store())
        sink.emit("some-site", "page_created", {"page_path": "x"})
        assert called == []
