"""Outbound webhook management (YOL-248).

Users register webhook URLs here; polling-worker reads them from Secrets
Manager and injects them as YOLOSCRIBE_WEBHOOKS into the agent-runner Job
container so agents can call put_notification without the LLM ever seeing
the raw URLs.

SM path: yoloscribe/{user_id}/webhooks
Schema:  JSON array of {"label": str, "url": str}
"""

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_user_id
from config import secrets_store

router = APIRouter()

_MAX_WEBHOOKS = 20
_MAX_LABEL_LEN = 100
_URL_RE = re.compile(r"^https?://\S+$")


class WebhookEntry(BaseModel):
    label: str = ""
    url: str


def _key(user_id: str) -> str:
    return f"yoloscribe/{user_id}/webhooks"


def _load(user_id: str) -> list[dict]:
    raw = secrets_store.get(_key(user_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []


def _save(user_id: str, webhooks: list[dict]) -> None:
    secrets_store.put(
        _key(user_id),
        json.dumps(webhooks),
        description=f"YoloScribe outbound webhooks for user {user_id}",
    )


@router.get(
    "/outbound-webhooks",
    tags=["webhooks"],
    summary="List outbound webhooks",
    description="Return the user's configured outbound webhook URLs.",
)
async def list_outbound_webhooks(user_id: str = Depends(get_user_id)) -> dict:
    entries = _load(user_id)
    return {
        "webhooks": [
            {"index": i, "label": e.get("label", ""), "url": e["url"]}
            for i, e in enumerate(entries)
        ]
    }


@router.post(
    "/outbound-webhooks",
    tags=["webhooks"],
    summary="Add an outbound webhook",
    description=(
        "Add a webhook URL to the user's outbound list. Any valid http/https URL works — "
        "Discord, Slack, Teams, or custom endpoints. Maximum 20 webhooks per user."
    ),
    status_code=201,
)
async def add_outbound_webhook(
    body: WebhookEntry,
    user_id: str = Depends(get_user_id),
) -> dict:
    url = body.url.strip()
    if not _URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must be a valid http:// or https:// URL")
    entries = _load(user_id)
    if len(entries) >= _MAX_WEBHOOKS:
        raise HTTPException(status_code=400, detail=f"Maximum of {_MAX_WEBHOOKS} webhooks allowed")
    entries.append({"label": body.label.strip()[:_MAX_LABEL_LEN], "url": url})
    _save(user_id, entries)
    return {"status": "added", "index": len(entries) - 1}


@router.delete(
    "/outbound-webhooks/{index}",
    tags=["webhooks"],
    summary="Delete an outbound webhook",
    description="Remove a webhook by its list index.",
)
async def delete_outbound_webhook(
    index: int,
    user_id: str = Depends(get_user_id),
) -> dict:
    entries = _load(user_id)
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="Webhook index out of range")
    entries.pop(index)
    _save(user_id, entries)
    return {"status": "deleted"}
