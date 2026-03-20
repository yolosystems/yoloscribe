from fastapi import APIRouter, HTTPException, Request

from auth import get_site_for_user
from aws.infra import provision_user_infrastructure
from config import WEBHOOK_SECRET
from models import UserCreatedEvent

router = APIRouter()


@router.post("/webhooks/user-created", tags=["webhooks"], summary="Webhook: provision infrastructure for a new user")
async def user_created(request: Request, event: UserCreatedEvent) -> dict[str, str]:
    """Provision IAM role, K8s ServiceAccount, and Secrets Manager placeholder for a new user."""
    secret = request.headers.get("x-webhook-secret", "")
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    site_name = get_site_for_user(event.user_id)
    if site_name is None:
        raise HTTPException(status_code=400, detail="No site found for user; provision a site first")

    try:
        await provision_user_infrastructure(event.user_id, site_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"status": "ok"}
