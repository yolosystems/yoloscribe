from fastapi import APIRouter, Depends

from auth import get_user_context
from config import token_budget_repo
from token_budget import DEFAULT_DAILY_LIMIT, _resets_at_utc

router = APIRouter()


@router.get(
    "/token-budget",
    tags=["token-budget"],
    summary="Get current token usage and daily budget",
    description=(
        "Returns today's token consumption, the daily limit, and the UTC reset time "
        "for the authenticated user. When token budgets are not configured on this server "
        "the limit is returned as the platform default and used is 0."
    ),
)
async def get_token_budget(
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    user_id, _ = ctx
    if token_budget_repo is None:
        return {"used": 0, "limit": DEFAULT_DAILY_LIMIT, "resets_at": _resets_at_utc()}
    used = token_budget_repo.get_used(user_id)
    limit = token_budget_repo.get_limit(user_id)
    return {"used": used, "limit": limit, "resets_at": _resets_at_utc()}
