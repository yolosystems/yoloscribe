"""POST /message — chat endpoint for messaging bot integrations.

Authenticated by API token (as_...). The bot passes {platform, channel_id,
message}; the server resolves the site from the token, loads conversation
history from the in-memory cache, calls MessagingAgent, and appends the
completed turn back to the cache.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from agents.messaging import MessagingAgent
from auth import get_user_context
from config import S3_BUCKET, s3, token_budget_repo
from message_history import append_history, get_history
from models import TokenBudgetInfo
from rate_limit import limiter
from token_budget import _resets_at_utc

router = APIRouter()

_messaging_agent = MessagingAgent(s3=s3, bucket=S3_BUCKET)


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
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=401, detail="API token is not associated with a site")

    if not req.platform.strip():
        raise HTTPException(status_code=400, detail="platform is required")
    if not req.channel_id.strip():
        raise HTTPException(status_code=400, detail="channel_id is required")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    budget_used = 0
    budget_limit = 0
    if token_budget_repo is not None:
        budget_used = token_budget_repo.get_used(user_id)
        budget_limit = token_budget_repo.get_limit(user_id)
        if budget_used >= budget_limit:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Daily token budget exhausted "
                    f"({budget_used:,} / {budget_limit:,} tokens used). "
                    f"Resets at UTC midnight."
                ),
            )

    history = get_history(user_id, req.platform, req.channel_id)

    try:
        reply, tokens_used = _messaging_agent.run(
            message=req.message,
            site=site,
            history=history,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    append_history(user_id, req.platform, req.channel_id, req.message, reply)

    token_budget: TokenBudgetInfo | None = None
    if token_budget_repo is not None:
        if tokens_used > 0:
            token_budget_repo.record_usage(user_id, tokens_used)
        token_budget = TokenBudgetInfo(
            used=budget_used + tokens_used,
            limit=budget_limit,
            resets_at=_resets_at_utc(),
        )

    return MessageResponse(reply=reply, token_budget=token_budget)
