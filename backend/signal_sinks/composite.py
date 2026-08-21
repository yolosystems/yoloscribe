"""CompositeSignalSink — fans a signal out to multiple sinks, isolating failures."""

from __future__ import annotations

import logging

from .base import SignalSink

log = logging.getLogger(__name__)


class CompositeSignalSink(SignalSink):
    """Emits to every configured sink; one sink's failure never affects the others."""

    def __init__(self, sinks: list[SignalSink]) -> None:
        self._sinks = sinks

    def emit(self, site: str, signal_type: str, payload: dict, user_id: str = "") -> None:
        for sink in self._sinks:
            try:
                sink.emit(site, signal_type, payload, user_id)
            except Exception as exc:
                log.warning("SignalSink %s failed for site %s: %s", type(sink).__name__, site, exc)
