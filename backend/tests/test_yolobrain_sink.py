"""YoloBrainSignalSink and actor threading (YOL-558).

Completes the transport whose receiving end is YoloBrain's POST /internal/signals
(YOL-557). Before this, KM signals were built correctly — `reason` populated
(YOL-527), accepted on arrival (YOL-550) — and then delivered nowhere.

The tests cluster around two things that are easy to get quietly wrong:

1. **The actor.** `SignalSink.emit` is site-keyed but YoloBrain routes by user.
   The subject has to come from the mutation event, and it has to be the
   *actor* rather than the site owner — otherwise a shared-write user's edit is
   filed against someone else's memory. A bug here produces plausible-looking
   signals attributed to the wrong person.
2. **Best-effort delivery.** This runs on a background thread beside a wiki
   write. A YoloBrain outage must cost signals and nothing else.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from signal_sinks import CompositeSignalSink, YoloBrainSignalSink, create_signal_sink
from signal_sinks.base import NullSignalSink


class _Captured:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200
        self.raise_on_post: Exception | None = None


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    """Stub httpx.Client so no socket is opened."""
    cap = _Captured()

    class _Resp:
        def raise_for_status(self) -> None:
            if cap.status >= 400:
                raise RuntimeError(f"HTTP {cap.status}")

    class _Client:
        def __init__(self, **_: object) -> None: ...
        def __enter__(self) -> "_Client":
            return self
        def __exit__(self, *_: object) -> None: ...
        def post(self, url: str, json: dict, headers: dict) -> _Resp:
            if cap.raise_on_post is not None:
                raise cap.raise_on_post
            cap.calls.append({"url": url, "json": json, "headers": headers})
            return _Resp()

    import signal_sinks.yolobrain as mod

    monkeypatch.setattr(mod.httpx, "Client", _Client)
    return cap


def _sink() -> YoloBrainSignalSink:
    return YoloBrainSignalSink("http://yolobrain-api:8080", "the-secret")


class TestDelivery:
    def test_posts_to_the_internal_endpoint(self, http: _Captured) -> None:
        _sink().emit("acme", "content_routed", {"page_type": "project"}, "user-abc")
        assert http.calls[0]["url"] == "http://yolobrain-api:8080/internal/signals"

    def test_trailing_slash_on_base_url_does_not_double(self, http: _Captured) -> None:
        YoloBrainSignalSink("http://yb:8080/", "s").emit("acme", "x", {}, "u")
        assert http.calls[0]["url"] == "http://yb:8080/internal/signals"

    def test_body_shape_matches_the_endpoint_contract(self, http: _Captured) -> None:
        params = {"page_type": "project", "reason": "user asked to trim the intro"}
        _sink().emit("acme", "content_routed", params, "user-abc")
        assert http.calls[0]["json"] == {
            "user_sub": "user-abc",
            "signal_type": "content_routed",
            "params": params,
        }

    def test_secret_is_sent_as_the_internal_auth_header(self, http: _Captured) -> None:
        _sink().emit("acme", "content_routed", {}, "user-abc")
        assert http.calls[0]["headers"]["X-Internal-Auth"] == "the-secret"


class TestActorRouting:
    """YoloBrain's workspace is a user, so a signal without one has nowhere to go."""

    def test_no_actor_skips_delivery(self, http: _Captured) -> None:
        """Sending an empty sub would file the signal under a workspace keyed on
        the empty string — quietly polluting a real user's memory with activity
        that is not theirs. No emitting path produces this today; it guards the
        empty default on S3Tools and any future caller that omits the actor."""
        _sink().emit("acme", "content_routed", {"page_type": "project"}, "")
        assert http.calls == []

    def test_default_actor_also_skips(self, http: _Captured) -> None:
        """A caller that never learned about the parameter must not deliver
        an unattributed signal by omission."""
        _sink().emit("acme", "content_routed", {})
        assert http.calls == []

    def test_the_supplied_actor_is_the_subject(self, http: _Captured) -> None:
        """Not the site owner — the person who made the change."""
        _sink().emit("owners-site", "content_routed", {}, "the-editor")
        assert http.calls[0]["json"]["user_sub"] == "the-editor"


class TestBestEffort:
    def test_http_error_does_not_raise(self, http: _Captured) -> None:
        """A YoloBrain outage degrades to lost signals, never a failed wiki write."""
        http.status = 503
        _sink().emit("acme", "content_routed", {}, "user-abc")  # must not raise

    def test_connection_failure_does_not_raise(self, http: _Captured) -> None:
        http.raise_on_post = OSError("connection refused")
        _sink().emit("acme", "content_routed", {}, "user-abc")  # must not raise

    def test_a_failing_yolobrain_does_not_stop_other_sinks(self, http: _Captured) -> None:
        """CompositeSignalSink isolates failures; pinned here because losing the
        generic webhook feature to a YoloBrain outage would be a silent
        cross-feature regression."""
        seen: list[str] = []

        class _Recorder(NullSignalSink):
            def emit(self, site: str, signal_type: str, payload: dict, user_id: str = "") -> None:
                seen.append(signal_type)

        http.raise_on_post = OSError("down")
        CompositeSignalSink([_sink(), _Recorder()]).emit("acme", "content_routed", {}, "u")
        assert seen == ["content_routed"]


class TestFactoryConfiguration:
    """Half-configured must mean off, not "try anyway"."""

    @staticmethod
    def _types(monkeypatch: pytest.MonkeyPatch, url: str | None, secret: str | None) -> list[str]:
        for name, val in (("YOLOBRAIN_API_URL", url), ("YOLOBRAIN_INTERNAL_SECRET", secret)):
            if val is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, val)
        sink = create_signal_sink(secrets_store=None)
        return [type(s).__name__ for s in sink._sinks]  # type: ignore[attr-defined]

    def test_both_set_enables_the_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "YoloBrainSignalSink" in self._types(monkeypatch, "http://yb:8080", "s")

    def test_neither_set_leaves_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "YoloBrainSignalSink" not in self._types(monkeypatch, None, None)

    def test_url_without_secret_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rather than POSTing unauthenticated to a real endpoint."""
        assert "YoloBrainSignalSink" not in self._types(monkeypatch, "http://yb:8080", None)

    def test_secret_without_url_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rather than authenticating against a default URL."""
        assert "YoloBrainSignalSink" not in self._types(monkeypatch, None, "s")

    def test_whitespace_only_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A blank Helm value renders as an empty string, not an absent var."""
        assert "YoloBrainSignalSink" not in self._types(monkeypatch, "   ", "  ")

    def test_webhook_sink_always_remains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "WebhookSignalSink" in self._types(monkeypatch, "http://yb:8080", "s")


class TestActorReachesTheSinkFromAMutation:
    """The seam that makes the whole thing work, end to end in-process.

    Each piece above is tested in isolation, but the actual requirement is that
    a wiki write performed by a user arrives at YoloBrain attributed to that
    user. That crosses WikiPageMarkdownFile → KMSignalHandler → dispatch, and
    the actor is dropped if any one of them forgets to forward it.
    """

    def test_wiki_write_carries_its_author_to_the_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import km_signal_handler
        from km_signal_handler import KMSignalHandler
        from yoloscribe_io import WikiPageMarkdownFile
        from yoloscribe_io.storage import LocalStorageBackend

        seen: list[tuple] = []
        monkeypatch.setattr(
            km_signal_handler, "dispatch", lambda *a: seen.append(a)
        )

        page = WikiPageMarkdownFile(site="acme", page_path="projects/x", storage=LocalStorageBackend())
        page.add_handler(KMSignalHandler())
        page.write("# X\n", user_id="the-editor", reason="trimming the intro")

        site, signal_type, params, actor = seen[0]
        assert site == "acme"
        assert signal_type == "content_routed"
        assert actor == "the-editor"
        # And the YOL-527 reason rides along in the same params.
        assert params["reason"] == "trimming the intro"

    def test_unattributed_write_yields_no_actor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A write with no author still reaches site-keyed sinks; only the
        user-routed one skips it. Not reachable from production code paths
        today — this pins the degradation, not a live case."""
        import km_signal_handler
        from km_signal_handler import KMSignalHandler
        from yoloscribe_io import WikiPageMarkdownFile
        from yoloscribe_io.storage import LocalStorageBackend

        seen: list[tuple] = []
        monkeypatch.setattr(km_signal_handler, "dispatch", lambda *a: seen.append(a))

        page = WikiPageMarkdownFile(site="acme", page_path="notes", storage=LocalStorageBackend())
        page.add_handler(KMSignalHandler())
        page.write("# X\n")

        assert seen[0][3] == ""
