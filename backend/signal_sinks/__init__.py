"""Signal-sink factory — wires the site-configurable knowledge-management signal fan-out.

See projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission in
the wiki for the design. NullSignalSink is the degenerate "nothing
configured" case; WebhookSignalSink is opt-in per site (no targets
configured = no-op); YoloBrainSignalSink will be added once YOL-491
(per-site YoloBrain auth) lands.
"""

from __future__ import annotations

from .base import NullSignalSink, SignalSink
from .composite import CompositeSignalSink
from .webhook import WebhookSignalSink

__all__ = [
    "SignalSink",
    "NullSignalSink",
    "WebhookSignalSink",
    "CompositeSignalSink",
    "create_signal_sink",
]


def create_signal_sink(secrets_store) -> SignalSink:
    """Build the process-wide SignalSink singleton.

    WebhookSignalSink is always included — it is a no-op for any site that
    hasn't configured a target, so there is no separate on/off switch here,
    matching outbound_webhooks.py's existing posture (YOL-248).
    """
    return CompositeSignalSink([WebhookSignalSink(secrets_store)])
