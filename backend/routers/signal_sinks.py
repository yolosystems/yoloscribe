"""Per-site signal-sink webhook configuration (YOL-495).

Site owners register generic webhook URLs here to receive the same
knowledge-management signals YoloScribe forwards to configured sinks — see
projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission.

SecretsStore path: yoloscribe/{site}/signal-sink-webhooks
Schema: JSON array of {"label": str, "url": str, "secret": str}
"""

import json
import re

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel

from auth import get_user_context, require_site_owner
from config import secrets_store
from signal_sinks.webhook import signal_sink_webhooks_key

router = APIRouter()

_MAX_TARGETS = 10
_MAX_LABEL_LEN = 100
_URL_RE = re.compile(r"^https?://\S+$")


class SignalSinkWebhookEntry(BaseModel):
    label: str = ""
    url: str
    secret: str = ""


def _load(site: str) -> list[dict]:
    raw = secrets_store.get(signal_sink_webhooks_key(site))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []


def _save(site: str, targets: list[dict]) -> None:
    secrets_store.put(
        signal_sink_webhooks_key(site),
        json.dumps(targets),
        description=f"YoloScribe signal-sink webhooks for site {site}",
    )


@router.get(
    "/signal-sinks/webhooks",
    tags=["signal-sinks"],
    summary="List signal-sink webhook targets",
    description="Return this site's configured webhook targets for knowledge-management signal forwarding. Owner only.",
)
async def list_signal_sink_webhooks(
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    _, user_site = ctx
    require_site_owner(site, user_site)
    entries = _load(site)
    return {
        "webhooks": [
            {"index": i, "label": e.get("label", ""), "url": e["url"], "has_secret": bool(e.get("secret"))}
            for i, e in enumerate(entries)
        ]
    }


@router.post(
    "/signal-sinks/webhooks",
    tags=["signal-sinks"],
    summary="Add a signal-sink webhook target",
    description=(
        "Add a webhook URL that receives this site's knowledge-management signals "
        f"(the same signal_type/payload shape forwarded to other sinks). Maximum {_MAX_TARGETS} per site."
    ),
    status_code=201,
)
async def add_signal_sink_webhook(
    body: SignalSinkWebhookEntry,
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    _, user_site = ctx
    require_site_owner(site, user_site)
    url = body.url.strip()
    if not _URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must be a valid http:// or https:// URL")
    entries = _load(site)
    if len(entries) >= _MAX_TARGETS:
        raise HTTPException(status_code=400, detail=f"Maximum of {_MAX_TARGETS} signal-sink webhooks allowed")
    entries.append({
        "label": body.label.strip()[:_MAX_LABEL_LEN],
        "url": url,
        "secret": body.secret.strip(),
    })
    _save(site, entries)
    return {"status": "added", "index": len(entries) - 1}


@router.delete(
    "/signal-sinks/webhooks/{index}",
    tags=["signal-sinks"],
    summary="Delete a signal-sink webhook target",
    description="Remove a webhook target by its list index. Owner only.",
)
async def delete_signal_sink_webhook(
    index: int,
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    _, user_site = ctx
    require_site_owner(site, user_site)
    entries = _load(site)
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="Webhook index out of range")
    entries.pop(index)
    _save(site, entries)
    return {"status": "deleted"}
