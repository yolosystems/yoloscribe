from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import trace as _ot
from opentelemetry.trace import StatusCode
from starlette.requests import Request

from yoloscribe_io import use_request_litellm_key

from agents import LibrarianAgent
from auth import get_user_context, require_site_owner
from config import S3_BUCKET, SQS_QUEUE_URL, api_token_repo, s3, secrets_store, sqs
from credentials import get_user_budget, load_litellm_key
from models import ChatRequest, ChatResponse, TokenBudgetInfo
from rate_limit import limiter
from path_safety import is_safe_path
from queue_helpers import enqueue_index_job

router = APIRouter()
_tracer = _ot.get_tracer("yoloscribe.chat")

# Lazily instantiated singleton — built on first request, not at import (so a
# missing LITELLM_BASE_URL doesn't break module import; see build_strands_model).
_chat_agent: LibrarianAgent | None = None


def _get_chat_agent() -> LibrarianAgent:
    global _chat_agent
    if _chat_agent is None:
        _chat_agent = LibrarianAgent(
            s3=s3,
            bucket=S3_BUCKET,
            sqs_client=sqs,
            sqs_queue_url=SQS_QUEUE_URL,
            secrets_store=secrets_store,
            api_token_repo=api_token_repo,
        )
    return _chat_agent


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

    # Budget enforcement is now LiteLLM's job (the user's virtual key returns 429
    # when exhausted); no pre-flight check here.
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant")
    ]

    with _tracer.start_as_current_span("yoloscribe.chat") as _span:
        _span.set_attribute("openinference.span.kind", "CHAIN")
        _span.set_attribute("user.id", user_id)
        _span.set_attribute("site", req.site)
        _span.set_attribute("page_path", req.file_path)
        if req.session_id:
            _span.set_attribute("session.id", req.session_id)
        _span.set_attribute("input.value", req.message)

        try:
            # Build the singleton first (its base model uses the shared key), then
            # bind the user's budgeted virtual key for this request's models (YOL-513).
            agent = _get_chat_agent()
            use_request_litellm_key(load_litellm_key(user_id))
            reply, updated_content, navigate_to, tokens_used = agent.run(
                message=req.message,
                current_content=req.current_content,
                history=history,
                site=req.site,
                file_path=req.file_path,
                user_id=user_id,
                user_site=user_site or "",
            )
        except PermissionError as exc:
            _span.set_status(StatusCode.ERROR, str(exc))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            _span.set_status(StatusCode.ERROR, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        _span.set_attribute("output.value", reply)
        _span.set_status(StatusCode.OK)

    if updated_content is not None:
        content_key = f"{req.site}/{req.file_path}"
        if req.file_path == "content.md" or req.file_path.endswith("/content.md"):
            enqueue_index_job(content_key, user_id)

    # Budget/usage is metered by LiteLLM on the user's key; surface it for the UI.
    budget = get_user_budget(user_id)
    token_budget = TokenBudgetInfo(**budget) if budget else None

    return ChatResponse(
        reply=reply,
        updated_content=updated_content,
        navigate_to=navigate_to,
        token_budget=token_budget,
    )
