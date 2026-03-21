"""Supabase PostgREST helpers for the Discord bot.

Only two operations are needed:
- Look up an api_tokens row by hash (setup flow — validates token + gets site_name/id)
- Upsert a discord_configs row (setup flow)
- Look up a discord_configs row by channel_id (message handling hot path)

All calls use the service role key which bypasses RLS.
"""

import json
import urllib.parse
import urllib.request

from discord_bot.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }


def get_api_token_by_hash(token_hash: str) -> dict | None:
    """Return the api_tokens row for a given hash, or None if not found/revoked/expired."""
    qs = urllib.parse.urlencode({
        "token_hash": f"eq.{token_hash}",
        "revoked_at": "is.null",
        "select": "id,site_name,expires_at",
        "limit": "1",
    })
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}",
        method="GET",
        headers=_headers(),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            rows = json.loads(resp.read())
            return rows[0] if rows else None
    except Exception:
        return None


def upsert_discord_config(
    channel_id: str,
    guild_id: str,
    api_token_id: str,
    encrypted_token: str,
) -> None:
    """Upsert a discord_configs row (insert or replace on channel_id conflict)."""
    row = {
        "channel_id": channel_id,
        "guild_id": guild_id,
        "api_token_id": api_token_id,
        "encrypted_token": encrypted_token,
    }
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/discord_configs",
        data=json.dumps(row).encode(),
        method="POST",
        headers={
            **_headers(),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req)


def get_discord_config(channel_id: str) -> dict | None:
    """Return the discord_configs row for a channel, or None if not configured."""
    qs = urllib.parse.urlencode({
        "channel_id": f"eq.{channel_id}",
        "select": "encrypted_token",
        "limit": "1",
    })
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/discord_configs?{qs}",
        method="GET",
        headers=_headers(),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            rows = json.loads(resp.read())
            return rows[0] if rows else None
    except Exception:
        return None
