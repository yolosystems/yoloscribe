"""Messaging connections REST endpoints.

Allows site owners to list and revoke their messaging_configs rows
(connected channels across all platforms) from the frontend UI.

Storage is reached through the provider-agnostic repositories
(config.api_token_repo / config.messaging_config_repo) rather than a direct
Supabase dependency. NOTE: the rows themselves are *written* by the separate
messaging-bot service; on a non-Supabase install its store must be migrated
before these listings return data.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import config
from auth import get_user_context, require_site_owner

router = APIRouter()


def _owned_token_ids_and_names(user_id: str, site: str) -> tuple[set[str], dict[str, str]]:
    """Return (token_ids, {id: name}) for the site's API tokens owned by this user."""
    if config.api_token_repo is None:
        return set(), {}
    tokens = [t for t in config.api_token_repo.list_tokens(user_id) if t.get("site_name") == site]
    names = {t["id"]: t["name"] for t in tokens}
    return set(names.keys()), names


@router.get("/messaging-configs", tags=["tools"], summary="List messaging channel connections")
async def list_messaging_configs(
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return all messaging channel connections for the authenticated site owner."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    if config.messaging_config_repo is None:
        return {"configs": []}

    token_ids, token_names = _owned_token_ids_and_names(user_id, site)
    if not token_ids:
        return {"configs": []}

    config_rows = config.messaging_config_repo.list_by_token_ids(list(token_ids))
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
    if config.messaging_config_repo is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    row = config.messaging_config_repo.get(config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Verify the config belongs to a token owned by this site before deleting.
    owned_token_ids, _ = _owned_token_ids_and_names(user_id, site)
    if row["api_token_id"] not in owned_token_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    config.messaging_config_repo.delete(config_id)
    return {"deleted": True}
