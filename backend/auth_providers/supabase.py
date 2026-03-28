"""Supabase implementations of AuthProvider, UserSiteRepository, and ApiTokenRepository."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx
import jwt as pyjwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .base import ApiTokenRepository, AuthProvider, JWTClaims, UserSiteRepository

log = logging.getLogger(__name__)


class SupabaseAuthProvider(AuthProvider):
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self._url = supabase_url
        self._key = supabase_key
        self._jwks = PyJWKClient(
            f"{supabase_url}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=600,
        )

    def decode_jwt(self, token: str) -> JWTClaims:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
            )
            return JWTClaims(user_id=payload["sub"], email=payload.get("email"))
        except pyjwt.exceptions.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    def get_authorize_url(self, redirect_uri: str, code_challenge: str) -> str:
        return (
            f"{self._url}/auth/v1/authorize"
            f"?provider=google"
            f"&code_challenge={urllib.parse.quote(code_challenge, safe='')}"
            f"&code_challenge_method=S256"
            f"&redirect_to={urllib.parse.quote(redirect_uri, safe='')}"
        )

    async def exchange_code(self, code: str, code_verifier: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._url}/auth/v1/token",
                params={"grant_type": "pkce"},
                json={"auth_code": code, "code_verifier": code_verifier},
                headers={"apikey": self._key},
            )
            resp.raise_for_status()
            return resp.json()

    async def refresh_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._url}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                json={"refresh_token": refresh_token},
                headers={"apikey": self._key},
            )
            resp.raise_for_status()
            return resp.json()

    def delete_user(self, user_id: str) -> None:
        req = urllib.request.Request(
            f"{self._url}/auth/v1/admin/users/{user_id}",
            method="DELETE",
            headers={
                "Authorization": f"Bearer {self._key}",
                "apikey": self._key,
            },
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Supabase Auth delete error: {exc}") from exc


class SupabaseUserSiteRepository(UserSiteRepository):
    _TTL = 300  # 5-minute cache

    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self._url = supabase_url
        self._key = supabase_key
        self._cache: dict[str, tuple[str | None, float]] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "apikey": self._key,
        }

    def get_site_for_user(self, user_id: str) -> str | None:
        now = time.time()
        if user_id in self._cache:
            site, ts = self._cache[user_id]
            if now - ts < self._TTL:
                return site

        url = f"{self._url}/rest/v1/user_site?user_uuid=eq.{user_id}&select=site_name&limit=1"
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read())
                site: str | None = data[0]["site_name"] if data else None
        except Exception:
            site = None

        if site is not None:
            self._cache[user_id] = (site, now)
        return site

    def insert_user_site(self, user_id: str, site_name: str, theme: str) -> None:
        data = json.dumps({"user_uuid": user_id, "site_name": site_name, "theme": theme}).encode()
        req = urllib.request.Request(
            f"{self._url}/rest/v1/user_site",
            data=data,
            method="POST",
            headers={
                **self._headers(),
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Supabase PostgREST error: {exc}") from exc

    def delete_user_site(self, user_id: str) -> None:
        req = urllib.request.Request(
            f"{self._url}/rest/v1/user_site?user_uuid=eq.{user_id}",
            method="DELETE",
            headers=self._headers(),
        )
        try:
            urllib.request.urlopen(req)
        except Exception as exc:
            log.warning("Failed to delete user_site row for %s: %s", user_id, exc)
        self._cache.pop(user_id, None)


class SupabaseApiTokenRepository(ApiTokenRepository):
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self._url = supabase_url
        self._key = supabase_key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "apikey": self._key,
            "Content-Type": "application/json",
        }

    def insert_token(
        self,
        user_id: str,
        site_name: str,
        name: str,
        token_hash: str,
        expires_at: str | None = None,
    ) -> str:
        row: dict = {"user_id": user_id, "site_name": site_name, "name": name, "token_hash": token_hash}
        if expires_at:
            row["expires_at"] = expires_at
        req = urllib.request.Request(
            f"{self._url}/rest/v1/api_tokens",
            data=json.dumps(row).encode(),
            method="POST",
            headers={**self._headers(), "Prefer": "return=representation"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())[0]["id"]
        except urllib.error.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Supabase error: {exc}") from exc

    def list_tokens(self, user_id: str) -> list[dict]:
        qs = urllib.parse.urlencode({
            "user_id": f"eq.{user_id}",
            "revoked_at": "is.null",
            "select": "id,name,site_name,created_at,expires_at,last_used_at",
            "order": "created_at.desc",
        })
        req = urllib.request.Request(
            f"{self._url}/rest/v1/api_tokens?{qs}",
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    def revoke_token(self, token_id: str, user_id: str) -> bool:
        qs = urllib.parse.urlencode({
            "id": f"eq.{token_id}",
            "user_id": f"eq.{user_id}",
            "revoked_at": "is.null",
        })
        req = urllib.request.Request(
            f"{self._url}/rest/v1/api_tokens?{qs}",
            data=json.dumps({"revoked_at": "now()"}).encode(),
            method="PATCH",
            headers={**self._headers(), "Prefer": "return=representation"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return len(json.loads(resp.read())) > 0
        except urllib.error.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Supabase error: {exc}") from exc

    def get_by_hash(self, token_hash: str) -> dict | None:
        qs = urllib.parse.urlencode({
            "token_hash": f"eq.{token_hash}",
            "revoked_at": "is.null",
            "select": "id,user_id,site_name,expires_at",
            "limit": "1",
        })
        req = urllib.request.Request(
            f"{self._url}/rest/v1/api_tokens?{qs}",
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                rows = json.loads(resp.read())
                return rows[0] if rows else None
        except Exception:
            return None

    def update_last_used(self, token_id: str) -> None:
        qs = urllib.parse.urlencode({"id": f"eq.{token_id}"})
        req = urllib.request.Request(
            f"{self._url}/rest/v1/api_tokens?{qs}",
            data=json.dumps({"last_used_at": "now()"}).encode(),
            method="PATCH",
            headers={**self._headers(), "Prefer": "return=minimal"},
        )
        try:
            urllib.request.urlopen(req)
        except Exception as exc:
            log.warning("Failed to update token last_used_at for %s: %s", token_id, exc)
