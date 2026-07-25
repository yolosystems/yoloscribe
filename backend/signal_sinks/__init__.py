"""Signal-sink factory — wires the site-configurable knowledge-management signal fan-out.

See projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission in
the wiki for the design. NullSignalSink is the degenerate "nothing
configured" case; WebhookSignalSink is opt-in per site (no targets
configured = no-op); YoloBrainSignalSink will be added once YOL-491
(per-site YoloBrain auth) lands.
"""

from __future__ import annotations

import asyncio
import logging

from .base import NullSignalSink, SignalSink
from .composite import CompositeSignalSink
from .webhook import WebhookSignalSink

log = logging.getLogger(__name__)

__all__ = [
    "SignalSink",
    "NullSignalSink",
    "WebhookSignalSink",
    "CompositeSignalSink",
    "create_signal_sink",
    "dispatch",
]


def dispatch(site: str, signal_type: str, params: dict) -> None:
    """Fan out a typed KM signal to the process-wide SignalSink — sink-only.

    The single shared entry point used by every server-side emission site
    (MCP tool bodies in mcp_server.py, REST proposal endpoints in
    routers/content.py). Kept off the write path: a site with no sinks
    configured pays nothing, and a sink that does blocking I/O
    (WebhookSignalSink POSTs) never delays the mutation it rides alongside —
    the dispatch is offloaded to a background thread when a running event loop
    is present. Best-effort; never raises. See
    projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission.
    """
    try:
        from config import signal_sink
    except Exception as exc:
        log.warning("KM SignalSink unavailable for %s/%s: %s", site, signal_type, exc)
        return

    def _run() -> None:
        try:
            signal_sink.emit(site, signal_type, params)
        except Exception as exc:
            log.warning("KM SignalSink dispatch failed for %s/%s: %s", site, signal_type, exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _run()
        return
    loop.run_in_executor(None, _run)


def create_signal_sink(secrets_store) -> SignalSink:
    """Build the process-wide SignalSink singleton.

    WebhookSignalSink is always included — it is a no-op for any site that
    hasn't configured a target, so there is no separate on/off switch here,
    matching outbound_webhooks.py's existing posture (YOL-248).
    """
    return CompositeSignalSink([WebhookSignalSink(secrets_store)])
