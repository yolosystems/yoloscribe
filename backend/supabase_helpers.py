"""Supabase admin API helpers (PostgREST + Auth admin)."""

import json
import logging
import urllib.error
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
