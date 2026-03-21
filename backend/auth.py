"""JWT authentication helpers for the AgentScribe API."""

import dataclasses
import datetime
import hashlib
import json
import urllib.request

import jwt as pyjwt
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, jwks_client
from supabase_helpers import supabase_get_api_token_by_hash, supabase_update_token_last_used

_bearer = HTTPBearer(auto_error=False)


@dataclasses.dataclass
class JWTClaims:
    user_id: str
    email: str | None


def decode_jwt(credentials: HTTPAuthorizationCredentials | None) -> JWTClaims:
    """Validate Supabase JWT and return user_id + email."""
    if jwks_client is None:
        raise HTTPException(status_code=500, detail="SUPABASE_URL is not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    token = credentials.credentials
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
        )
        return JWTClaims(user_id=payload["sub"], email=payload.get("email"))
    except pyjwt.exceptions.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc


def get_site_for_user(user_id: str) -> str | None:
    """Look up the user's site name from the user_site table via Supabase PostgREST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/user_site?user_uuid=eq.{user_id}&select=site_name&limit=1"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            return data[0]["site_name"] if data else None
    except Exception:
        return None


def get_user_id(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> str:
    """Extract user_id from JWT (backwards-compatible for /secrets routes)."""
    return decode_jwt(credentials).user_id


def resolve_api_token(raw_token: str) -> tuple[str, str | None]:
    """Validate an `as_`-prefixed API token and return (user_id, site_name).

    Raises HTTPException(401) if the token is unknown, revoked, or expired.
    Updates last_used_at in the background (best-effort, never raises).
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = supabase_get_api_token_by_hash(token_hash)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API token")
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry < datetime.datetime.now(datetime.timezone.utc):
                raise HTTPException(status_code=401, detail="API token has expired")
        except ValueError:
            pass  # Unparseable expiry — treat as non-expiring
    supabase_update_token_last_used(row["id"])
    return row["user_id"], row.get("site_name")


def get_user_context(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> tuple[str, str | None]:
    """Extract user_id + site_name from a Supabase JWT or an `as_`-prefixed API token."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    raw = credentials.credentials
    if raw.startswith("as_"):
        return resolve_api_token(raw)
    claims = decode_jwt(credentials)
    return claims.user_id, get_site_for_user(claims.user_id)


def get_jwt_claims(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JWTClaims:
    """Extract and validate JWT, returning full claims including email."""
    return decode_jwt(credentials)


def require_site_owner(requested_site: str, user_site: str | None) -> None:
    if user_site is None or user_site != requested_site:
        raise HTTPException(status_code=403, detail="Access denied: not your site")
