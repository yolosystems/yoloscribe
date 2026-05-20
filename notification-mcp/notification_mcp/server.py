"""Stateless outbound notification MCP server.

Reads YOLOSCRIBE_WEBHOOKS (JSON-encoded list of webhook URL strings) from the
process environment and POSTs the caller's message to every URL.

Runs as an stdio subprocess inside the agent-runner K8s Job, inheriting its
container env. YOLOSCRIBE_WEBHOOKS is injected by polling-worker at job
creation time — the LLM agent never sees the URLs.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)
mcp = FastMCP("notification-mcp")


def _load_webhooks() -> list[str]:
    raw = os.environ.get("YOLOSCRIBE_WEBHOOKS", "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("YOLOSCRIBE_WEBHOOKS is not valid JSON — no webhooks loaded")
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return []


@mcp.tool()
def put_notification(message: str) -> str:
    """Send a notification message to all of the user's configured webhook URLs.

    Use this to alert the user when a significant event occurs — a long-running
    task has completed, an error needs attention, or important content has changed.
    Keep the message concise (1-3 sentences) and include enough context for the
    user to know what triggered it.
    """
    webhooks = _load_webhooks()
    if not webhooks:
        return "No webhooks configured — message not delivered. The user has not set up any outbound webhooks."

    results: list[str] = []
    with httpx.Client(timeout=10.0) as client:
        for url in webhooks:
            try:
                resp = client.post(url, json={"content": message})
                resp.raise_for_status()
                results.append(f"delivered (HTTP {resp.status_code})")
            except httpx.HTTPStatusError as exc:
                results.append(f"failed (HTTP {exc.response.status_code})")
                log.warning("Webhook delivery failed for %s: %s", url[:60], exc)
            except Exception as exc:
                results.append(f"failed ({exc})")
                log.warning("Webhook delivery error for %s: %s", url[:60], exc)

    delivered = sum(1 for r in results if r.startswith("delivered"))
    total = len(results)
    summary = f"Delivered {delivered}/{total} notifications."
    if delivered < total:
        summary += " Some deliveries failed:\n" + "\n".join(
            f"  {r}" for r in results if not r.startswith("delivered")
        )
    return summary


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
