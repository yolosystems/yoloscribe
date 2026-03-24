"""JWT authentication helpers for the YoloScribe API."""

import datetime
import hashlib

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_providers.base import JWTClaims  # re-exported for call-site backwards compat
from config import LOCAL_MODE, LOCAL_SITE_NAME, LOCAL_USER_ID, auth_provider, api_token_repo, user_site_repo

_bearer = HTTPBearer(auto_error=False)

__all__ = ["JWTClaims", "decode_jwt", "get_site_for_user", "get_user_context",
           "get_jwt_claims", "get_user_id", "require_site_owner", "_bearer"]


def decode_jwt(credentials: HTTPAuthorizationCredentials | None) -> JWTClaims:
    """Validate a JWT and return user_id + email."""
    if LOCAL_MODE:
        return JWTClaims(user_id=LOCAL_USER_ID, email="local@localhost")
    if auth_provider is None:
        raise HTTPException(status_code=500, detail="Auth provider is not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    return auth_provider.decode_jwt(credentials.credentials)


def get_site_for_user(user_id: str) -> str | None:
    """Look up the user's site name."""
    if LOCAL_MODE:
        return LOCAL_SITE_NAME
    if user_site_repo is None:
        return None
    return user_site_repo.get_site_for_user(user_id)


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
    if api_token_repo is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API token")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = api_token_repo.get_by_hash(token_hash)
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
    api_token_repo.update_last_used(row["id"])
    return row["user_id"], row.get("site_name")


def get_user_context(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> tuple[str, str | None]:
    """Extract user_id + site_name from a JWT or an `as_`-prefixed API token."""
    if LOCAL_MODE:
        return LOCAL_USER_ID, LOCAL_SITE_NAME
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
