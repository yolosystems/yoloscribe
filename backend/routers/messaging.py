"""Messaging connections REST endpoints.

Allows site owners to list and revoke their messaging_configs rows
(connected channels across all platforms) from the frontend UI.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException

from auth import get_user_context, require_site_owner
from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

router = APIRouter()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }


def _get(url: str) -> list:
    req = urllib.request.Request(url, method="GET", headers=_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


@router.get("/messaging-configs", tags=["tools"], summary="List messaging channel connections")
async def list_messaging_configs(
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return all messaging channel connections for the authenticated site owner."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    # Get all API token IDs for this site
    qs = urllib.parse.urlencode({
        "site_name": f"eq.{site}",
        "revoked_at": "is.null",
        "select": "id,name",
    })
    token_rows = _get(f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}")
    if not token_rows:
        return {"configs": []}

    token_ids = [r["id"] for r in token_rows]
    token_names = {r["id"]: r["name"] for r in token_rows}

    # Get messaging_configs for those tokens
    qs2 = urllib.parse.urlencode({
        "api_token_id": f"in.({','.join(token_ids)})",
        "select": "id,platform,connection,created_at,api_token_id",
    })
    config_rows = _get(f"{SUPABASE_URL}/rest/v1/messaging_configs?{qs2}")

    configs = [
        {
            "id": r["id"],
            "platform": r["platform"],
            "connection": r["connection"],
            "created_at": r["created_at"],
            "api_token_id": r["api_token_id"],
            "api_token_name": token_names.get(r["api_token_id"], "—"),
        }
        for r in config_rows
    ]
    return {"configs": configs}


@router.delete("/messaging-config", tags=["tools"], summary="Revoke a messaging channel connection")
async def delete_messaging_config(
    site: str = "default",
    config_id: str = "",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Revoke a messaging channel connection by ID. Requires site ownership."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    if not config_id:
        raise HTTPException(status_code=400, detail="config_id is required")

    # Verify the config belongs to a token owned by this site before deleting
    qs = urllib.parse.urlencode({
        "id": f"eq.{config_id}",
        "select": "id,api_token_id",
        "limit": "1",
    })
    rows = _get(f"{SUPABASE_URL}/rest/v1/messaging_configs?{qs}")
    if not rows:
        raise HTTPException(status_code=404, detail="Connection not found")

    token_id = rows[0]["api_token_id"]
    token_qs = urllib.parse.urlencode({
        "id": f"eq.{token_id}",
        "site_name": f"eq.{site}",
        "select": "id",
        "limit": "1",
    })
    token_rows = _get(f"{SUPABASE_URL}/rest/v1/api_tokens?{token_qs}")
    if not token_rows:
        raise HTTPException(status_code=403, detail="Access denied")

    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/messaging_configs?id=eq.{urllib.parse.quote(config_id)}",
        method="DELETE",
        headers=_headers(),
    )
    urllib.request.urlopen(req)
    return {"deleted": True}
