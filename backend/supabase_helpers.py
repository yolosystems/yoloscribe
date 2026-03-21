"""Supabase admin API helpers (PostgREST + Auth admin)."""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


def supabase_insert_user_site(user_id: str, site_name: str, theme: str) -> None:
    """Insert into user_site table via Supabase PostgREST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    url = f"{SUPABASE_URL}/rest/v1/user_site"
    data = json.dumps({"user_uuid": user_id, "site_name": site_name, "theme": theme}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase PostgREST error: {exc}") from exc


def supabase_delete_user_site(user_id: str) -> None:
    """Delete from user_site table via Supabase PostgREST. Logs warning on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/user_site?user_uuid=eq.{user_id}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        urllib.request.urlopen(req)
    except Exception as exc:
        logging.warning("Failed to delete user_site row for %s: %s", user_id, exc)


def supabase_delete_auth_user(user_id: str) -> None:
    """Delete Supabase Auth user. Raises HTTPException(502) on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    url = f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase Auth delete error: {exc}") from exc


# ── API token helpers ──────────────────────────────────────────────────────────


def _supa_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }


def supabase_insert_api_token(
    user_id: str,
    site_name: str,
    name: str,
    token_hash: str,
    expires_at: str | None = None,
) -> str:
    """Insert a new api_tokens row and return its UUID."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    row: dict = {"user_id": user_id, "site_name": site_name, "name": name, "token_hash": token_hash}
    if expires_at:
        row["expires_at"] = expires_at
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens",
        data=json.dumps(row).encode(),
        method="POST",
        headers={**_supa_headers(), "Prefer": "return=representation"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())[0]["id"]
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase error: {exc}") from exc


def supabase_list_api_tokens(user_id: str) -> list[dict]:
    """Return all non-revoked api_tokens rows for a user (excludes hash)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return []
    qs = urllib.parse.urlencode({
        "user_id": f"eq.{user_id}",
        "revoked_at": "is.null",
        "select": "id,name,site_name,created_at,expires_at,last_used_at",
        "order": "created_at.desc",
    })
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}",
        method="GET",
        headers=_supa_headers(),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def supabase_revoke_api_token(token_id: str, user_id: str) -> bool:
    """Set revoked_at on an api_tokens row owned by user_id. Returns True if found."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase admin API not configured")
    qs = urllib.parse.urlencode({"id": f"eq.{token_id}", "user_id": f"eq.{user_id}", "revoked_at": "is.null"})
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}",
        data=json.dumps({"revoked_at": "now()"}).encode(),
        method="PATCH",
        headers={**_supa_headers(), "Prefer": "return=representation"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return len(json.loads(resp.read())) > 0
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase error: {exc}") from exc


def supabase_get_api_token_by_hash(token_hash: str) -> dict | None:
    """Look up an active (non-revoked, non-expired) api_tokens row by hash."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    qs = urllib.parse.urlencode({
        "token_hash": f"eq.{token_hash}",
        "revoked_at": "is.null",
        "select": "id,user_id,site_name,expires_at",
        "limit": "1",
    })
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}",
        method="GET",
        headers=_supa_headers(),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            rows = json.loads(resp.read())
            return rows[0] if rows else None
    except Exception:
        return None


def supabase_update_token_last_used(token_id: str) -> None:
    """Update last_used_at to now() for an api_tokens row. Best-effort; never raises."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    qs = urllib.parse.urlencode({"id": f"eq.{token_id}"})
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/api_tokens?{qs}",
        data=json.dumps({"last_used_at": "now()"}).encode(),
        method="PATCH",
        headers={**_supa_headers(), "Prefer": "return=minimal"},
    )
    try:
        urllib.request.urlopen(req)
    except Exception as exc:
        logging.warning("Failed to update token last_used_at for %s: %s", token_id, exc)
