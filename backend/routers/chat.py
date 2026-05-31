from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request

from agents import ChatAgent
from auth import get_user_context, require_site_owner
from config import S3_BUCKET, SQS_QUEUE_URL, api_token_repo, s3, secrets_store, sqs, token_budget_repo
from models import ChatRequest, ChatResponse, TokenBudgetInfo
from rate_limit import limiter
from path_safety import is_safe_path
from queue_helpers import enqueue_index_job
from token_budget import _resets_at_utc

router = APIRouter()

# Module-level singleton — instantiated once at startup.
_chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
    secrets_store=secrets_store,
    api_token_repo=api_token_repo,
)


@router.post(
    "/chat",
    tags=["chat"],
    summary="Chat with the AI agent",
    description=(
        "Send a user message to the ChatAgent orchestrator. The agent may read/write "
        "page content, create agents, create pages, or enqueue async runner jobs. "
        "Requires site ownership. Returns the agent's reply and optionally updated content "
        "or a navigation target."
    ),
    response_model=ChatResponse,
)
@limiter.limit("10/minute")
@limiter.limit("100/hour")
async def chat(
    request: Request,
    req: ChatRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> Any:
    user_id, user_site = ctx
    require_site_owner(req.site, user_site)
    if not is_safe_path(req.file_path):
        raise HTTPException(status_code=400, detail="Invalid file_path")

    # Pre-flight budget check — reject before touching the LLM if exhausted.
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

    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant")
    ]

    try:
        reply, updated_content, navigate_to, tokens_used = _chat_agent.run(
            message=req.message,
            current_content=req.current_content,
            history=history,
            site=req.site,
            file_path=req.file_path,
            user_id=user_id,
            user_site=user_site or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if updated_content is not None:
        content_key = f"{req.site}/{req.file_path}"
        if req.file_path == "content.md" or req.file_path.endswith("/content.md"):
            enqueue_index_job(content_key, user_id)

    # Record usage and build budget response.
    token_budget: TokenBudgetInfo | None = None
    if token_budget_repo is not None:
        if tokens_used > 0:
            token_budget_repo.record_usage(user_id, tokens_used)
        new_used = budget_used + tokens_used
        token_budget = TokenBudgetInfo(
            used=new_used,
            limit=budget_limit,
            resets_at=_resets_at_utc(),
        )

    return ChatResponse(
        reply=reply,
        updated_content=updated_content,
        navigate_to=navigate_to,
        token_budget=token_budget,
    )
