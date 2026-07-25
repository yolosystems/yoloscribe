"""WebhookSignalSink — forwards signals to per-site-configured generic webhook targets.

Targets are stored in SecretsStore at yoloscribe/{site}/signal-sink-webhooks as
a JSON array of {"label": str, "url": str, "secret": str}, managed via
GET/POST/DELETE /signal-sinks/webhooks (backend/routers/signal_sinks.py) — the
same JSON-array-in-SecretsStore convention as outbound_webhooks.py (YOL-248).

A site with no targets configured is a no-op — this is what makes the
feature opt-in per site with no separate global on/off switch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from .base import SignalSink

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


def signal_sink_webhooks_key(site: str) -> str:
    return f"yoloscribe/{site}/signal-sink-webhooks"


class WebhookSignalSink(SignalSink):
    """POSTs {signal_type, payload, site, at} JSON to a site's configured webhook targets."""

    def __init__(self, secrets_store) -> None:
        self._secrets_store = secrets_store

    def emit(self, site: str, signal_type: str, payload: dict) -> None:
        targets = self._load_targets(site)
        if not targets:
            return
        body = {
            "signal_type": signal_type,
            "payload": payload,
            "site": site,
            "at": datetime.now(tz=timezone.utc).isoformat(),
        }
        for target in targets:
            self._post_one(target, body)

    def _load_targets(self, site: str) -> list[dict]:
        raw = self._secrets_store.get(signal_sink_webhooks_key(site))
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupt signal-sink-webhooks config for site %s", site)
            return []

    def _post_one(self, target: dict, body: dict) -> None:
        url = target.get("url", "")
        if not url:
            return
        headers = {"Content-Type": "application/json"}
        secret = target.get("secret", "")
        if secret:
            headers["X-Signal-Secret"] = secret
        try:
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=body, headers=headers)
                resp.raise_for_status()
        except Exception as exc:
            log.warning("WebhookSignalSink delivery to %s failed: %s", target.get("label") or url, exc)
