"""POST /message — chat endpoint for messaging bot integrations.

Authenticated by API token (as_...). The bot passes {platform, channel_id,
message}; the server resolves the site from the token, loads conversation
history from the in-memory cache, calls MessagingAgent, and appends the
completed turn back to the cache.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import trace as _ot
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from starlette.requests import Request

from yoloscribe_io import use_request_litellm_key

from agents.messaging import MessagingAgent
from auth import get_user_context
from config import S3_BUCKET, s3
from credentials import get_user_budget, load_litellm_key
from message_history import append_history, get_history
from models import TokenBudgetInfo
from rate_limit import limiter

log = logging.getLogger(__name__)
router = APIRouter()
_tracer = _ot.get_tracer("yoloscribe.message")

# Lazily instantiated singleton — built on first request, not at import (so a
# missing LITELLM_BASE_URL doesn't break module import).
_messaging_agent: MessagingAgent | None = None


def _get_messaging_agent() -> MessagingAgent:
    global _messaging_agent
    if _messaging_agent is None:
        _messaging_agent = MessagingAgent(s3=s3, bucket=S3_BUCKET)
    return _messaging_agent


class MessageRequest(BaseModel):
    platform: str
    channel_id: str
    message: str


class MessageResponse(BaseModel):
    reply: str
    token_budget: TokenBudgetInfo | None = None


@router.post(
    "/message",
    tags=["chat"],
    summary="Send a message via a messaging bot integration",
    description=(
        "Stateless messaging endpoint for Discord, Slack, and other platform bots. "
        "Authenticated by an `as_`-prefixed API token. The server resolves the site "
        "from the token, loads per-channel conversation history from an in-memory cache, "
        "and calls the MessagingAgent (a Q&A-oriented agent with search and multi-page "
        "read/write tools). Returns a plain-text reply."
    ),
    response_model=MessageResponse,
)
@limiter.limit("20/minute")
@limiter.limit("200/hour")
async def message(
    request: Request,
    req: MessageRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> MessageResponse:
    return await handle_message(ctx, req)


async def handle_message(
    ctx: tuple[str, str | None],
    req: MessageRequest,
) -> MessageResponse:
    """Core message handling, independent of how the caller was authenticated.

    Shared with the internal messaging endpoint (YOL-523), which resolves the
    caller from a channel binding instead of a bearer token. Kept separate from
    the route so the internal path doesn't inherit the IP-keyed rate limit —
    all bot traffic arrives from one pod IP, so a per-IP limit there would let
    one busy channel throttle every other site.
    """
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=401, detail="API token is not associated with a site")

    if not req.platform.strip():
        raise HTTPException(status_code=400, detail="platform is required")
    if not req.channel_id.strip():
        raise HTTPException(status_code=400, detail="channel_id is required")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    # Budget enforcement is now LiteLLM's job (the user's key returns 429 when
    # exhausted); no pre-flight check here.

    history = get_history(user_id, req.platform, req.channel_id)

    with _tracer.start_as_current_span("yoloscribe.message") as _span:
        _span.set_attribute("openinference.span.kind", "CHAIN")
        _span.set_attribute("user.id", user_id)
        _span.set_attribute("site", site)
        _span.set_attribute("session.id", f"{req.platform}:{req.channel_id}")
        _span.set_attribute("input.value", req.message)

        try:
            agent = _get_messaging_agent()
            use_request_litellm_key(load_litellm_key(user_id))  # user's budgeted key (YOL-513)
            reply, tokens_used = agent.run(
                message=req.message,
                site=site,
                history=history,
                user_id=user_id,
            )
        except Exception as exc:
            _span.set_status(StatusCode.ERROR, str(exc))
            # Log before raising: the detail only reaches the HTTP response body,
            # and callers (notably the messaging bot) discard it — leaving a bare
            # "502 Bad Gateway" access-log line as the only trace of the failure.
            log.exception(
                "MessagingAgent failed for user=%s site=%s %s:%s — %s",
                user_id, site, req.platform, req.channel_id, exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        _span.set_attribute("output.value", reply)
        _span.set_status(StatusCode.OK)

    append_history(user_id, req.platform, req.channel_id, req.message, reply)

    budget = get_user_budget(user_id)
    token_budget = TokenBudgetInfo(**budget) if budget else None

    return MessageResponse(reply=reply, token_budget=token_budget)
