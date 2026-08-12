"""Internal, backend-to-backend endpoints. Not part of the public API surface."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
import run_tokens
from config import mcp_api_base
from internal_auth import check_caller, check_messaging_bot
from routers.ingest import trigger_ingest, upload_ingest
from routers.message import MessageRequest, MessageResponse, handle_message

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


# ── Messaging bot (YOL-523) ───────────────────────────────────────────────────
#
# The messaging bot holds no database access, no encryption key, and no user API
# tokens. It authenticates with its own secret and names a channel; the backend
# resolves channel → api_token_id → (user_id, site) and runs the request as that
# owner. A user's API token is therefore never handled outside this process.


class LinkChannelRequest(BaseModel):
    platform: str
    channel_id: str
    api_token: str
    connection: dict = {}


class LinkChannelResponse(BaseModel):
    site_name: str
    config_id: str


class ChannelRef(BaseModel):
    platform: str
    channel_id: str


def _resolve_channel(platform: str, channel_id: str) -> tuple[str, str]:
    """Resolve a platform channel to (user_id, site_name) via its API-token binding.

    Raises 404 when the channel isn't linked, or when its token has since been
    revoked or expired — revoking a token disconnects its channels by construction.
    """
    if config.messaging_config_repo is None or config.api_token_repo is None:
        raise HTTPException(status_code=404, detail="Channel is not linked")

    binding = config.messaging_config_repo.get_by_channel(platform, channel_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="Channel is not linked")

    token_row = config.api_token_repo.get_by_id(binding["api_token_id"])
    if token_row is None:
        raise HTTPException(status_code=404, detail="Channel is not linked")

    expires_at = token_row.get("expires_at")
    if expires_at and _is_expired(expires_at):
        raise HTTPException(status_code=404, detail="Channel is not linked")

    site_name = token_row.get("site_name")
    if not site_name:
        raise HTTPException(status_code=404, detail="Channel is not linked")
    return token_row["user_id"], site_name


def _is_expired(expires_at: str) -> bool:
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False  # Unparseable expiry — treat as non-expiring, as auth.py does
    return expiry < datetime.now(tz=timezone.utc)


@router.post(
    "/internal/messaging/link",
    tags=["internal"],
    summary="Bind a chat channel to the site behind an API token",
)
async def link_channel(
    req: LinkChannelRequest,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> LinkChannelResponse:
    """Called during the bot's /setup flow, with the token the user just pasted.

    The raw token is validated here and deliberately not stored: the binding
    keeps only api_token_id, so there is no credential at rest to encrypt.
    """
    check_messaging_bot(x_internal_auth)

    if not req.platform.strip() or not req.channel_id.strip():
        raise HTTPException(status_code=400, detail="platform and channel_id are required")
    if config.api_token_repo is None or config.messaging_config_repo is None:
        raise HTTPException(status_code=503, detail="Messaging storage is not configured")

    token_hash = hashlib.sha256(req.api_token.encode()).hexdigest()
    token_row = config.api_token_repo.get_by_hash(token_hash)
    if token_row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API token")

    expires_at = token_row.get("expires_at")
    if expires_at and _is_expired(expires_at):
        raise HTTPException(status_code=401, detail="API token has expired")

    site_name = token_row.get("site_name")
    if not site_name:
        raise HTTPException(status_code=400, detail="API token is not associated with a site")

    connection = {**req.connection, "channel_id": req.channel_id}
    config_id = config.messaging_config_repo.upsert(req.platform, token_row["id"], connection)
    log.info("Linked %s channel %s to site %s", req.platform, req.channel_id, site_name)
    return LinkChannelResponse(site_name=site_name, config_id=config_id)


@router.get(
    "/internal/messaging/binding",
    tags=["internal"],
    summary="Check whether a chat channel is linked",
)
async def get_binding(
    platform: str,
    channel_id: str,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> dict:
    """Report whether a channel is linked, so the bot knows to respond.

    Returns the site name only — never a credential.
    """
    check_messaging_bot(x_internal_auth)
    try:
        _user_id, site = _resolve_channel(platform, channel_id)
    except HTTPException:
        return {"linked": False}
    return {"linked": True, "site_name": site}


@router.post(
    "/internal/messaging/message",
    tags=["internal"],
    summary="Run a chat message against the site bound to this channel",
)
async def internal_message(
    req: MessageRequest,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> MessageResponse:
    check_messaging_bot(x_internal_auth)
    ctx = _resolve_channel(req.platform, req.channel_id)
    return await handle_message(ctx, req)


@router.post(
    "/internal/messaging/ingest/upload",
    tags=["internal"],
    summary="Request a pre-signed ingest upload URL for this channel's site",
)
async def internal_ingest_upload(
    filename: str,
    req: ChannelRef,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> dict:
    check_messaging_bot(x_internal_auth)
    ctx = _resolve_channel(req.platform, req.channel_id)
    return await upload_ingest(filename=filename, ctx=ctx)


@router.post(
    "/internal/messaging/ingest/trigger",
    tags=["internal"],
    summary="Trigger ingest agents for this channel's site",
)
async def internal_ingest_trigger(
    req: ChannelRef,
    x_internal_auth: str = Header(default="", alias="X-Internal-Auth"),
) -> dict:
    check_messaging_bot(x_internal_auth)
    ctx = _resolve_channel(req.platform, req.channel_id)
    return await trigger_ingest(ctx=ctx)
