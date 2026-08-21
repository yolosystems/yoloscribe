"""Abstract base class for YoloScribe's pluggable signal-sink interfaces.

A SignalSink forwards knowledge-management signals — the same signal_type +
payload shape recorded locally by the Librarian's SignalLog (see
backend/mcp_server.py::_emit_signal) — to an external destination: YoloBrain,
a generic webhook, or (the default) nowhere at all. See
projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission in the
wiki for the design rationale.

Emission is best-effort by contract: implementations must not raise on
failure, so that a sink outage never blocks the mutation or local
SignalLog write it rides alongside.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SignalSink(ABC):
    """Forwards a knowledge-management signal for a site to an external destination."""

    @abstractmethod
    def emit(self, site: str, signal_type: str, payload: dict, user_id: str = "") -> None:
        """Forward a signal. Best-effort: must not raise on failure.

        `user_id` is the *actor* — whoever performed the mutation, which for a
        shared-write user editing someone else's page is the editor rather than
        the site owner (YOL-558). Sinks that route by site can ignore it; sinks
        that route by user require it and should skip when it is empty rather
        than inventing a subject.

        Keyword-defaulted rather than required so existing site-keyed sinks are
        unaffected, and so a caller with no actor in hand degrades to "no
        user-routed delivery" instead of a TypeError on a best-effort path.
        """


class NullSignalSink(SignalSink):
    """No-op sink — the degenerate case for a site with nothing configured."""

    def emit(self, site: str, signal_type: str, payload: dict, user_id: str = "") -> None:
        return None
