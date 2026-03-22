import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException

from agents.base import tools_prefix
from auth import get_user_id, get_user_context
from config import S3_BUCKET, s3, sm
from credentials import (
    VAR_NAME_RE,
    get_aws_sso_client_config,
    get_tool_auth_type,
    get_user_settings,
    load_oauth_token,
    oauth_secret_id,
    save_user_settings,
    secret_id,
    secret_exists,
    tool_required_vars,
)
from models import SecretValue

router = APIRouter()


@router.get("/tools", tags=["tools"], summary="List all tools with per-user status")
async def get_tools(ctx: tuple[str, str | None] = Depends(get_user_context)) -> dict:
    """Return all tools with enabled state and credential status for this user."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    prefix = tools_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    tool_names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    settings = get_user_settings(user_site)
    enabled_tools: list[str] = settings.get("enabled_tools", [])

    _aws_sso_token: dict | None = None
    _aws_sso_token_loaded = False
    _aws_sso_config: dict | None = None
    _aws_sso_config_loaded = False

    def _get_aws_sso_token() -> dict | None:
        nonlocal _aws_sso_token, _aws_sso_token_loaded
        if not _aws_sso_token_loaded:
            _aws_sso_token = load_oauth_token(user_id, "aws-sso")
            _aws_sso_token_loaded = True
        return _aws_sso_token

    def _get_sso_config() -> tuple[bool, str | None, str | None]:
        nonlocal _aws_sso_config, _aws_sso_config_loaded
        if not _aws_sso_config_loaded:
            _aws_sso_config = get_aws_sso_client_config(user_site or "")
            _aws_sso_config_loaded = True
        if _aws_sso_config and _aws_sso_config.get("sso_start_url"):
            sso_r = _aws_sso_config.get("sso_region", "us-east-1")
            return (
                True,
                _aws_sso_config.get("sso_start_url"),
                sso_r,
                _aws_sso_config.get("aws_region", sso_r),
            )
        return False, None, None, None

    tools_out: dict = {}
    for tool_name in tool_names:
        enabled = tool_name in enabled_tools
        auth_type = get_tool_auth_type(tool_name)

        if auth_type == "aws-sso":
            token = _get_aws_sso_token() if enabled else None
            configured, sso_start_url_val, sso_region_val, aws_region_val = _get_sso_config()
            if enabled and token:
                expires_at = token.get("expires_at")
                expires_str = (
                    datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
                    if expires_at else None
                )
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": True,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": True,
                    "account_id": token.get("account_id"),
                    "role_name": token.get("role_name"),
                    "expires_at": expires_str,
                }
            elif enabled:
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": True,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": False,
                    "account_id": None,
                    "role_name": None,
                    "expires_at": None,
                }
            else:
                tools_out[tool_name] = {
                    "type": "aws-sso",
                    "enabled": False,
                    "configured": configured,
                    "sso_start_url": sso_start_url_val,
                    "sso_region": sso_region_val,
                    "aws_region": aws_region_val,
                    "authenticated": False,
                    "account_id": None,
                    "role_name": None,
                    "expires_at": None,
                }

        elif auth_type in ("oauth", "none"):
            if enabled:
                token = load_oauth_token(user_id, tool_name)
                if token:
                    expires_at = token.get("expires_at")
                    expires_str = (
                        datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc).isoformat()
                        if expires_at else None
                    )
                    tools_out[tool_name] = {
                        "type": "oauth",
                        "enabled": True,
                        "authenticated": True,
                        "expires_at": expires_str,
                        "scope": token.get("scope") or None,
                    }
                else:
                    tools_out[tool_name] = {
                        "type": "oauth",
                        "enabled": True,
                        "authenticated": False,
                        "expires_at": None,
                        "scope": None,
                    }
            else:
                tools_out[tool_name] = {
                    "type": "oauth",
                    "enabled": False,
                    "authenticated": False,
                    "expires_at": None,
                    "scope": None,
                }

        else:  # "key" — stdio tool
            if enabled:
                vars_needed = tool_required_vars(tool_name)
                stored = {v: secret_exists(user_id, v) for v in vars_needed}
                tools_out[tool_name] = {"type": "key", "enabled": True, "vars": vars_needed, "stored": stored}
            else:
                tools_out[tool_name] = {"type": "key", "enabled": False, "vars": [], "stored": {}}

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
        try:
            sid = oauth_secret_id(user_id, tool_name)
            sm.delete_secret(SecretId=sid, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            logging.warning("Failed to delete OAuth token for tool %s user %s: %s", tool_name, user_id, exc)

    for var_name in tool_required_vars(tool_name):
        try:
            sm.delete_secret(SecretId=secret_id(user_id, var_name), ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            logging.warning("Failed to delete secret %s for user %s: %s", var_name, user_id, exc)

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
    sid = secret_id(user_id, var_name)
    try:
        sm.put_secret_value(SecretId=sid, SecretString=body.value)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=sid,
            SecretString=body.value,
            Description=f"YoloScribe credential: {var_name} for user {user_id}",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "stored"}
