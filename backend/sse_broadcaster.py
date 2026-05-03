"""In-process SSE event broadcaster for the Obsidian sync API.

Maintains a set of asyncio.Queue per site. Works correctly for single-process
deployments (local dev, single-replica EKS). Multi-replica production would
need Redis pub/sub — the plugin falls back to /obsidian/changes polling in
that case since missed SSE events are non-fatal.
"""

import asyncio
import logging

_clients: dict[str, set[asyncio.Queue]] = {}  # site → set of per-connection queues

_log = logging.getLogger(__name__)


def register(site: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _clients.setdefault(site, set()).add(q)
    return q


def unregister(site: str, q: asyncio.Queue) -> None:
    _clients.get(site, set()).discard(q)


def broadcast(site: str, event: str, data: dict) -> None:
    """Push an SSE event to all connected clients for a site (best-effort)."""
    import json
    queues = _clients.get(site, set())
    if not queues:
        return
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(queues):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            _log.debug("SSE queue full for site %s, dropping event", site)
