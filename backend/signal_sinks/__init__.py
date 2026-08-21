"""Signal-sink factory — wires the site-configurable knowledge-management signal fan-out.

See projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission in
the wiki for the design. NullSignalSink is the degenerate "nothing
configured" case; WebhookSignalSink is opt-in per site (no targets
configured = no-op); YoloBrainSignalSink delivers to YoloBrain's internal
endpoint when YOLOBRAIN_API_URL and YOLOBRAIN_INTERNAL_SECRET are both set
(YOL-558).
"""

from __future__ import annotations

import asyncio
import logging
import os

from .base import NullSignalSink, SignalSink
from .composite import CompositeSignalSink
from .webhook import WebhookSignalSink
from .yolobrain import YoloBrainSignalSink

log = logging.getLogger(__name__)

__all__ = [
    "SignalSink",
    "NullSignalSink",
    "WebhookSignalSink",
    "YoloBrainSignalSink",
    "CompositeSignalSink",
    "create_signal_sink",
    "dispatch",
]


def dispatch(site: str, signal_type: str, params: dict, user_id: str = "") -> None:
    """Fan out a typed KM signal to the process-wide SignalSink — sink-only.

    The single shared entry point used by every server-side emission site
    (MCP tool bodies in mcp_server.py, REST proposal endpoints in
    routers/content.py). Kept off the write path: a site with no sinks
    configured pays nothing, and a sink that does blocking I/O
    (WebhookSignalSink POSTs) never delays the mutation it rides alongside —
    the dispatch is offloaded to a background thread when a running event loop
    is present. Best-effort; never raises. See
    projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission.

    `user_id` is the actor behind the mutation. Site-keyed sinks ignore it;
    YoloBrainSignalSink routes on it, because YoloBrain's workspace is a user
    rather than a site (YOL-558). Defaulted so callers with no actor in hand
    still emit to the site-keyed sinks.
    """
    try:
        from config import signal_sink
    except Exception as exc:
        log.warning("KM SignalSink unavailable for %s/%s: %s", site, signal_type, exc)
        return

    def _run() -> None:
        try:
            signal_sink.emit(site, signal_type, params, user_id)
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

    YoloBrainSignalSink is added only when both YOLOBRAIN_API_URL and
    YOLOBRAIN_INTERNAL_SECRET are set. Requiring both means a half-configured
    deployment emits nothing rather than POSTing unauthenticated to a URL, or
    authenticating against a default one. An install without YoloBrain sets
    neither and pays nothing.
    """
    sinks: list[SignalSink] = [WebhookSignalSink(secrets_store)]

    base_url = os.environ.get("YOLOBRAIN_API_URL", "").strip()
    secret = os.environ.get("YOLOBRAIN_INTERNAL_SECRET", "").strip()
    if base_url and secret:
        sinks.append(YoloBrainSignalSink(base_url, secret))
        log.info("YoloBrainSignalSink enabled → %s", base_url)
    elif base_url or secret:
        log.warning(
            "YoloBrain signal delivery is half-configured (%s set, %s missing) — "
            "no signals will be delivered to YoloBrain.",
            "YOLOBRAIN_API_URL" if base_url else "YOLOBRAIN_INTERNAL_SECRET",
            "YOLOBRAIN_INTERNAL_SECRET" if base_url else "YOLOBRAIN_API_URL",
        )

    return CompositeSignalSink(sinks)
