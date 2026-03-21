"""API token management endpoints (YOL-21, YOL-22, YOL-29)."""

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_jwt_claims, JWTClaims, get_user_context
from supabase_helpers import (
    supabase_insert_api_token,
    supabase_list_api_tokens,
    supabase_revoke_api_token,
)

router = APIRouter()

_TOKEN_PREFIX = "as_"
_TOKEN_BYTES = 32  # 256 bits of entropy → 64 hex chars


def _generate_token() -> str:
    """Return a new cryptographically random API token."""
    return _TOKEN_PREFIX + secrets.token_hex(_TOKEN_BYTES)


def _hash_token(raw: str) -> str:
    """Return the sha256 hex digest of a raw API token."""
    return hashlib.sha256(raw.encode()).hexdigest()


class TokenCreateRequest(BaseModel):
    name: str
    expires_at: str | None = None  # ISO-8601 datetime string, or null


class TokenCreateResponse(BaseModel):
    id: str
    name: str
    token: str  # Raw token — shown once, never stored


class TokenListItem(BaseModel):
    id: str
    name: str
    site_name: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None


@router.post(
    "/tokens",
    tags=["tokens"],
    summary="Create a new API token",
    description=(
        "Generate a new site-scoped API token. The raw token is returned once and "
        "cannot be retrieved again — only its sha256 hash is stored. "
        "Token format: `as_<64 random hex chars>`."
    ),
    response_model=TokenCreateResponse,
    status_code=201,
)
async def create_token(
    body: TokenCreateRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> TokenCreateResponse:
    user_id, site_name = ctx
    if not site_name:
        raise HTTPException(status_code=403, detail="You must provision a site before creating API tokens")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Token name must not be empty")

    raw = _generate_token()
    token_id = supabase_insert_api_token(
        user_id=user_id,
        site_name=site_name,
        name=body.name.strip(),
        token_hash=_hash_token(raw),
        expires_at=body.expires_at,
    )
    return TokenCreateResponse(id=token_id, name=body.name.strip(), token=raw)


@router.get(
    "/tokens",
    tags=["tokens"],
    summary="List API tokens",
    description="Return all active (non-revoked) API tokens for the authenticated user. Token hashes are never included.",
    response_model=list[TokenListItem],
)
async def list_tokens(
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> list[TokenListItem]:
    user_id, _ = ctx
    rows = supabase_list_api_tokens(user_id)
    return [TokenListItem(**row) for row in rows]


@router.delete(
    "/tokens/{token_id}",
    tags=["tokens"],
    summary="Revoke an API token",
    description="Immediately revoke an API token. The token is rejected on all subsequent requests.",
    status_code=200,
)
async def revoke_token(
    token_id: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict[str, str]:
    user_id, _ = ctx
    found = supabase_revoke_api_token(token_id=token_id, user_id=user_id)
    if not found:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"status": "revoked"}
