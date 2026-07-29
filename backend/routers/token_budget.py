from fastapi import APIRouter, Depends

from auth import get_user_context
from credentials import get_user_budget

router = APIRouter()


@router.get(
    "/token-budget",
    tags=["token-budget"],
    summary="Get the current LiteLLM spend and budget",
    description=(
        "Returns the authenticated user's LiteLLM virtual-key usage: `used` (spend), "
        "`limit` (max_budget), and `resets_at` (budget reset time). Values are in "
        "LiteLLM's budget unit ($ by default), not tokens. Returns zeros when the user "
        "has no provisioned key or the proxy is unavailable."
    ),
)
async def get_token_budget(
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    user_id, _ = ctx
    budget = get_user_budget(user_id)
    if budget is None:
        return {"used": 0, "limit": None, "resets_at": ""}
    return budget
