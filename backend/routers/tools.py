import datetime

from fastapi import APIRouter, Depends, HTTPException

from auth import get_user_id, get_user_context
from config import secrets_store
from credentials import (
    VAR_NAME_RE,
    get_tool_auth_type,
    get_user_settings,
    load_oauth_token,
    oauth_secret_id,
    save_user_settings,
    secret_id,
    tool_required_vars,
)
from litellm_keys import list_mcp_servers
from models import SecretValue

router = APIRouter()


@router.get("/tools", tags=["tools"], summary="List OAuth tools with per-user status")
async def get_tools(ctx: tuple[str, str | None] = Depends(get_user_context)) -> dict:
    """Return the OAuth tools available on this install with per-user connection status.

    The catalog comes from the LiteLLM MCP gateway (admin-managed servers with
    ``auth_type: oauth2``), not the legacy ``.tools/`` registry (YOL-505). Per-user
    status (enabled + stored token) is joined from the user's settings + Secrets
    Manager, exactly as before. The tool name is the LiteLLM ``server_name`` — it
    must match the name used for enrollment and the SM key ``.../oauth/{name}``.
    """
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    settings = get_user_settings(user_site)
    enabled_tools: list[str] = settings.get("enabled_tools", [])

    tools_out: dict = {}
    for srv in list_mcp_servers():
        # OAuth servers are the ones with a user-connect flow (delegated PKCE →
        # auth_type "oauth2"); tolerate "oauth"/"oauth2" spelling. Servers with no
        # auth (static key / none) aren't user-connectable, so they're skipped.
        if not str(srv.get("auth_type") or "").lower().startswith("oauth"):
            continue
        tool_name = srv.get("server_name")
        if not tool_name:
            continue
        enabled = tool_name in enabled_tools
        token = load_oauth_token(user_id, tool_name) if enabled else None
        expires_at = token.get("expires_at") if token else None
        expires_str = (
            datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
            if expires_at else None
        )
        tools_out[tool_name] = {
            "type": "oauth",
            "enabled": enabled,
            "authenticated": bool(token),
            "expires_at": expires_str,
            "scope": (token.get("scope") if token else None) or None,
        }

    return {"tools": tools_out}


@router.post("/tools/{tool_name}/enable", tags=["tools"], summary="Enable a tool for the current user")
async def enable_tool(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict[str, str]:
    """Add tool_name to the user's enabled_tools list in their site settings."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")
    settings = get_user_settings(user_site)
    enabled: list[str] = settings.get("enabled_tools", [])
    if tool_name not in enabled:
        enabled.append(tool_name)
        settings["enabled_tools"] = enabled
        save_user_settings(user_site, settings)
    return {"status": "enabled"}


@router.post("/tools/{tool_name}/disable", tags=["tools"], summary="Disable a tool for the current user")
async def disable_tool(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict[str, str]:
    """Remove tool_name from the user's enabled_tools list and delete stored credentials."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    settings = get_user_settings(user_site)
    enabled: list[str] = settings.get("enabled_tools", [])
    if tool_name in enabled:
        enabled.remove(tool_name)
        settings["enabled_tools"] = enabled
        save_user_settings(user_site, settings)

    if get_tool_auth_type(tool_name) not in ("aws-sso",):
        secrets_store.delete(oauth_secret_id(user_id, tool_name))

    for var_name in tool_required_vars(tool_name):
        secrets_store.delete(secret_id(user_id, var_name))

    return {"status": "disabled"}


@router.get("/secrets/status", tags=["secrets"], summary="Get credential status for all tools (legacy alias)")
async def get_secrets_status(ctx: tuple[str, str | None] = Depends(get_user_context)) -> dict:
    """Return all tools with their credential status for this user.

    This is an alias for GET /tools kept for backwards compatibility.
    """
    return await get_tools(ctx)


@router.put("/secrets/{var_name}", tags=["secrets"], summary="Store or update a credential")
async def put_secret(
    var_name: str,
    body: SecretValue,
    user_id: str = Depends(get_user_id),
) -> dict[str, str]:
    """Store or update a credential value in Secrets Manager for the current user."""
    if not VAR_NAME_RE.match(var_name):
        raise HTTPException(status_code=400, detail="Invalid variable name")
    try:
        secrets_store.put(
            secret_id(user_id, var_name),
            body.value,
            description=f"YoloScribe credential: {var_name} for user {user_id}",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "stored"}
