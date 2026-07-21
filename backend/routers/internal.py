"""Internal, backend-to-backend endpoints. Not part of the public API surface."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header
from pydantic import BaseModel

import run_tokens
from config import mcp_api_base
from internal_auth import check_caller

log = logging.getLogger(__name__)

router = APIRouter()


class MintRunTokenRequest(BaseModel):
    site: str
    user_id: str
    agent_name: str
    agent_type: str
    page_path: str = ""
    ttl_seconds: int | None = None


class MintRunTokenResponse(BaseModel):
    token: str
    expires_at: str
    mcp_url: str


@router.post("/internal/runs/mint", tags=["internal"], summary="Mint a scoped run token for an agent-runner job")
async def mint_run_token_endpoint(
    req: MintRunTokenRequest,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> MintRunTokenResponse:
    """Mint a short-lived, scoped run token. Called by polling_worker.py at job dispatch time.

    See projects/yoloscribe/ideas/delegation-token in the wiki for the full design.
    """
    check_caller(x_internal_auth)
    ttl_seconds = req.ttl_seconds or run_tokens.DEFAULT_TTL_SECONDS
    token = run_tokens.mint_run_token(
        site=req.site,
        user_id=req.user_id,
        agent_name=req.agent_name,
        agent_type=req.agent_type,
        page_path=req.page_path,
        ttl_seconds=ttl_seconds,
    )
    expires_at = datetime.now(tz=timezone.utc).timestamp() + ttl_seconds
    return MintRunTokenResponse(
        token=token,
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        mcp_url=f"{mcp_api_base()}/mcp/v1",
    )
