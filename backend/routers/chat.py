from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agents import ChatAgent
from auth import get_user_context, require_site_owner
from config import S3_BUCKET, SQS_QUEUE_URL, s3, sm, sqs
from models import ChatRequest, ChatResponse
from s3_helpers import enqueue_index_job, is_safe_path

router = APIRouter()

# Module-level singleton — instantiated once at startup.
_chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
    sm_client=sm,
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
async def chat(
    req: ChatRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> Any:
    user_id, user_site = ctx
    require_site_owner(req.site, user_site)
    if not is_safe_path(req.file_path):
        raise HTTPException(status_code=400, detail="Invalid file_path")

    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant")
    ]

    try:
        reply, updated_content, navigate_to = _chat_agent.run(
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

    return ChatResponse(reply=reply, updated_content=updated_content, navigate_to=navigate_to)
