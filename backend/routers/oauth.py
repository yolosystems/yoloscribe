import json
import logging
import secrets
import time

import boto3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from mcp_oauth import PKCEChallenge, build_authorization_url, discover, dynamic_client_registration
from mcp_oauth.discovery import AuthorizationServerMetadata
from mcp_oauth.oauth_flow import exchange_code

from agents.base import tools_prefix
from auth import get_user_context
from config import FRONTEND_URL, OAUTH_REDIRECT_URI, S3_BUCKET, boto_session, s3, secrets_store
from credentials import (
    get_aws_sso_client_config,
    get_tool_auth_type,
    load_oauth_token,
    load_platform_client_secret,
    load_tool_oauth_client,
    oauth_secret_id,
    save_aws_sso_client_config,
    save_oauth_token,
)
from state import (
    AwsSsoPendingState,
    OAuthPendingState,
    aws_sso_pending,
    cleanup_aws_sso_state,
    cleanup_oauth_state,
    oauth_pending,
)

router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────────

class _AwsSsoSetupRequest(BaseModel):
    sso_start_url: str
    sso_region: str
    aws_region: str = ""


class _AwsSsoSelectRoleRequest(BaseModel):
    account_id: str
    role_name: str


# ── OAuth (skill auth) ─────────────────────────────────────────────────────────

@router.post("/oauth/initiate/{tool_name}", tags=["oauth"], summary="Initiate OAuth flow for a tool")
async def oauth_initiate(
    tool_name: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Begin the OAuth flow for a remote MCP tool."""
    user_id, user_site = ctx
    if user_site is None:
        raise HTTPException(status_code=403, detail="No site provisioned for this user")

    if tool_name == "aws-sso":
        return await _initiate_aws_sso(user_id, user_site)

    key = f"{tools_prefix()}/{tool_name}/mcp.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        mcp_config = json.loads(obj["Body"].read())
    except Exception:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found or has no mcp.json")

    server_url: str | None = None
    for server_cfg in mcp_config.get("mcpServers", {}).values():
        if "url" in server_cfg:
            server_url = server_cfg["url"]
            break
    if not server_url:
        raise HTTPException(status_code=400, detail=f"Tool '{tool_name}' is not a remote MCP tool")

    cleanup_oauth_state()

    pre_registered = load_tool_oauth_client(tool_name)

    auth_meta: AuthorizationServerMetadata | None = None
    if pre_registered and pre_registered.get("authorization_endpoint") and pre_registered.get("token_endpoint"):
        auth_meta = AuthorizationServerMetadata(
            issuer=pre_registered.get("issuer", pre_registered["authorization_endpoint"]),
            authorization_endpoint=pre_registered["authorization_endpoint"],
            token_endpoint=pre_registered["token_endpoint"],
            registration_endpoint=pre_registered.get("registration_endpoint"),
            scopes_supported=pre_registered.get("scopes", []),
            code_challenge_methods_supported=pre_registered.get("code_challenge_methods_supported", ["S256"]),
        )
    else:
        try:
            _prm, auth_meta = await discover(server_url)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OAuth discovery failed for {server_url}: {exc}") from exc
        if auth_meta is None:
            raise HTTPException(status_code=502, detail=f"No OAuth authorization server found for {server_url}")

    if pre_registered:
        client_id: str = pre_registered["client_id"]
        client_secret: str | None = load_platform_client_secret(tool_name)
        scopes: list[str] = pre_registered.get("scopes") or (
            list(auth_meta.scopes_supported) if auth_meta.scopes_supported else []
        )
    else:
        if not auth_meta.registration_endpoint:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Tool '{tool_name}' has no pre-registered OAuth client "
                    f"and the MCP server at {server_url} does not support "
                    f"Dynamic Client Registration. Upload an oauth_client.json "
                    f"to .tools/{tool_name}/ in S3 with the pre-registered client_id."
                ),
            )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                client_data = await dynamic_client_registration(
                    client,
                    auth_meta.registration_endpoint,
                    OAUTH_REDIRECT_URI,
                    "YoloScribe",
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Dynamic client registration failed: {exc}") from exc

        client_id = client_data["client_id"]
        client_secret = client_data.get("client_secret")
        scopes = list(auth_meta.scopes_supported) if auth_meta.scopes_supported else []

    pkce = PKCEChallenge()
    state = secrets.token_urlsafe(32)

    auth_url = build_authorization_url(
        metadata=auth_meta,
        client_id=client_id,
        redirect_uri=OAUTH_REDIRECT_URI,
        pkce=pkce,
        state=state,
        scopes=scopes,
        resource=server_url,
    )

    oauth_pending[state] = OAuthPendingState(
        tool_name=tool_name,
        user_id=user_id,
        site=user_site,
        server_url=server_url,
        pkce_verifier=pkce.verifier,
        client_id=client_id,
        client_secret=client_secret,
        auth_metadata={
            "issuer": auth_meta.issuer,
            "authorization_endpoint": auth_meta.authorization_endpoint,
            "token_endpoint": auth_meta.token_endpoint,
            "registration_endpoint": auth_meta.registration_endpoint,
            "scopes_supported": auth_meta.scopes_supported,
            "code_challenge_methods_supported": auth_meta.code_challenge_methods_supported,
        },
        created_at=time.time(),
    )

    return {"auth_url": auth_url}


@router.get("/oauth/callback", tags=["oauth"], summary="OAuth authorization code callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Receive the OAuth authorization code callback and exchange it for tokens."""
    cleanup_oauth_state()

    def _frontend_redirect(site: str, params: str) -> RedirectResponse:
        return RedirectResponse(url=f"{FRONTEND_URL}/{site}?{params}", status_code=302)

    if error:
        pending = oauth_pending.get(state or "")
        site = pending.site if pending else "default"
        return _frontend_redirect(site, f"oauth_error={error_description or error}")

    if not state or state not in oauth_pending:
        return Response(  # type: ignore[return-value]
            content="Invalid or expired OAuth state. Please try authenticating again.",
            status_code=400,
        )

    pending = oauth_pending.pop(state)

    auth_meta = AuthorizationServerMetadata(
        issuer=pending.auth_metadata["issuer"],
        authorization_endpoint=pending.auth_metadata["authorization_endpoint"],
        token_endpoint=pending.auth_metadata["token_endpoint"],
        registration_endpoint=pending.auth_metadata.get("registration_endpoint"),
        scopes_supported=pending.auth_metadata.get("scopes_supported", []),
        code_challenge_methods_supported=pending.auth_metadata.get("code_challenge_methods_supported", []),
    )

    class _VerifierOnly:
        def __init__(self, verifier: str) -> None:
            self.verifier = verifier

    try:
        token_data = await exchange_code(
            metadata=auth_meta,
            code=code or "",
            redirect_uri=OAUTH_REDIRECT_URI,
            client_id=pending.client_id,
            pkce=_VerifierOnly(pending.pkce_verifier),  # type: ignore[arg-type]
            client_secret=pending.client_secret,
        )
    except Exception as exc:
        logging.warning("OAuth code exchange failed for user %s tool %s: %s", pending.user_id, pending.tool_name, exc)
        return _frontend_redirect(pending.site, f"oauth_error={exc}")

    token_blob = {
        "client_id": pending.client_id,
        "client_secret": pending.client_secret,
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": int(time.time()) + int(token_data.get("expires_in", 3600)),
        "token_type": token_data.get("token_type", "Bearer"),
        "scope": token_data.get("scope", ""),
        "server_url": pending.server_url,
        "auth_server_metadata": pending.auth_metadata,
    }

    try:
        save_oauth_token(pending.user_id, pending.tool_name, token_blob)
    except Exception as exc:
        logging.error("Failed to store OAuth token for user %s tool %s: %s", pending.user_id, pending.tool_name, exc)
        return _frontend_redirect(pending.site, "oauth_error=Failed+to+store+token")

    return _frontend_redirect(pending.site, f"oauth_success={pending.tool_name}")


# ── AWS SSO ────────────────────────────────────────────────────────────────────

async def _initiate_aws_sso(user_id: str, site: str) -> dict:
    """Start the AWS SSO device authorization flow."""
    config = get_aws_sso_client_config(site)
    if not config:
        raise HTTPException(
            status_code=400,
            detail="AWS SSO is not configured. Use GET/PUT /aws-sso/setup to set your SSO start URL and region.",
        )
    sso_start_url: str = config.get("sso_start_url", "").strip().rstrip("/")
    sso_region: str = config.get("sso_region", "us-east-1").strip()
    aws_region: str = config.get("aws_region", sso_region).strip()
    if not sso_start_url:
        raise HTTPException(status_code=400, detail="sso_start_url is missing from AWS SSO config.")

    logging.info("Starting AWS SSO device auth: start_url=%r region=%r", sso_start_url, sso_region)

    oidc = boto3.client("sso-oidc", region_name=sso_region)

    try:
        reg = oidc.register_client(clientName="YoloScribe", clientType="public")
    except Exception as exc:
        logging.error("RegisterClient failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AWS SSO client registration failed: {exc}") from exc

    client_id: str = reg["clientId"]
    client_secret: str = reg["clientSecret"]
    logging.info("RegisterClient OK: client_id=%r", client_id)

    try:
        auth = oidc.start_device_authorization(
            clientId=client_id,
            clientSecret=client_secret,
            startUrl=sso_start_url,
        )
    except Exception as exc:
        logging.error(
            "StartDeviceAuthorization failed (start_url=%r region=%r): %s",
            sso_start_url, sso_region, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"AWS SSO device authorization failed (start_url={sso_start_url!r}, region={sso_region!r}): {exc}",
        ) from exc

    cleanup_aws_sso_state()
    state = secrets.token_urlsafe(32)
    aws_sso_pending[state] = AwsSsoPendingState(
        user_id=user_id,
        site=site,
        sso_region=sso_region,
        sso_start_url=sso_start_url,
        aws_region=aws_region,
        client_id=client_id,
        client_secret=client_secret,
        device_code=auth["deviceCode"],
        created_at=time.time(),
        expires_in=auth.get("expiresIn", 600),
        interval=auth.get("interval", 5),
    )

    return {
        "auth_url": auth["verificationUriComplete"],
        "user_code": auth.get("userCode", ""),
        "session": state,
        "polling_interval": auth.get("interval", 5),
        "expires_in": auth.get("expiresIn", 600),
    }


@router.get("/aws-sso/setup", tags=["oauth"], summary="Get AWS SSO configuration for a site")
async def aws_sso_get_setup(
    site: str = Query(...),
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return the current AWS SSO configuration for the site."""
    user_id, user_site = ctx
    if user_site is None or user_site != site:
        raise HTTPException(status_code=403, detail="Access denied")
    return get_aws_sso_client_config(site) or {}


@router.put("/aws-sso/setup", tags=["oauth"], summary="Save AWS SSO configuration for a site")
async def aws_sso_put_setup(
    body: _AwsSsoSetupRequest,
    site: str = Query(...),
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Write sso_start_url and sso_region to {site}/.aws-sso/aws-sso-client.json."""
    user_id, user_site = ctx
    if user_site is None or user_site != site:
        raise HTTPException(status_code=403, detail="Access denied")
    save_aws_sso_client_config(site, {
        "sso_start_url": body.sso_start_url,
        "sso_region": body.sso_region,
        "aws_region": body.aws_region or body.sso_region,
    })
    return {"status": "ok"}


@router.get("/aws-sso/auth-status", tags=["oauth"], summary="Poll AWS SSO device authorization status")
async def aws_sso_auth_status(
    session: str = Query(...),
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Poll for completion of the AWS SSO device authorization flow."""
    user_id, _ = ctx

    cleanup_aws_sso_state()
    pending = aws_sso_pending.get(session)
    if not pending:
        raise HTTPException(status_code=404, detail="Session not found or expired. Please re-authenticate.")
    if pending.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    oidc = boto_session.client("sso-oidc", region_name=pending.sso_region)
    try:
        token_response = oidc.create_token(
            clientId=pending.client_id,
            clientSecret=pending.client_secret,
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=pending.device_code,
        )
    except oidc.exceptions.AuthorizationPendingException:
        return {"status": "pending"}
    except oidc.exceptions.SlowDownException:
        return {"status": "pending"}
    except oidc.exceptions.ExpiredTokenException:
        del aws_sso_pending[session]
        return {"status": "expired"}
    except Exception as exc:
        del aws_sso_pending[session]
        logging.warning("AWS SSO token exchange failed for user %s: %s", user_id, exc)
        return {"status": "error", "error": str(exc)}

    access_token: str = token_response["accessToken"]
    del aws_sso_pending[session]

    pending_blob = {
        "access_token": access_token,
        "refresh_token": token_response.get("refreshToken"),
        "expires_at": int(time.time()) + int(token_response.get("expiresIn", 3600)),
        "client_id": pending.client_id,
        "client_secret": pending.client_secret,
        "sso_region": pending.sso_region,
        "sso_start_url": pending.sso_start_url,
        "aws_region": pending.aws_region,
    }
    save_oauth_token(user_id, "aws-sso-pending", pending_blob)

    sso_client = boto_session.client("sso", region_name=pending.sso_region)
    try:
        accounts_resp = sso_client.list_accounts(accessToken=access_token, maxResults=100)
        accounts = [
            {
                "account_id": a["accountId"],
                "account_name": a["accountName"],
                "email": a.get("emailAddress", ""),
            }
            for a in accounts_resp.get("accountList", [])
        ]
    except Exception as exc:
        logging.warning("Failed to list SSO accounts for user %s: %s", user_id, exc)
        accounts = []

    return {"status": "authorized", "accounts": accounts}


@router.get("/aws-sso/roles/{account_id}", tags=["oauth"], summary="List roles for an AWS SSO account")
async def aws_sso_list_roles(
    account_id: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return the permission set roles available in an AWS account for the pending SSO token."""
    user_id, _ = ctx
    token = load_oauth_token(user_id, "aws-sso-pending")
    if not token:
        raise HTTPException(status_code=404, detail="No pending AWS SSO session. Please re-authenticate.")

    sso_region: str = token.get("sso_region", "us-east-1")
    sso_client = boto_session.client("sso", region_name=sso_region)
    try:
        roles_resp = sso_client.list_account_roles(
            accessToken=token["access_token"],
            accountId=account_id,
            maxResults=100,
        )
        roles = [{"role_name": r["roleName"]} for r in roles_resp.get("roleList", [])]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list roles: {exc}") from exc

    return {"roles": roles}


@router.post("/aws-sso/select-role", tags=["oauth"], summary="Confirm AWS account and role selection")
async def aws_sso_select_role(
    body: _AwsSsoSelectRoleRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Attach the chosen account_id and role_name to the pending token and promote it to active."""
    user_id, _ = ctx
    token = load_oauth_token(user_id, "aws-sso-pending")
    if not token:
        raise HTTPException(status_code=404, detail="No pending AWS SSO session. Please re-authenticate.")

    sso_region: str = token.get("sso_region", "us-east-1")
    sso_client = boto_session.client("sso", region_name=sso_region)

    try:
        sso_client.get_role_credentials(
            accountId=body.account_id,
            roleName=body.role_name,
            accessToken=token["access_token"],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not get credentials for {body.role_name} in account {body.account_id}: {exc}",
        ) from exc

    token["account_id"] = body.account_id
    token["role_name"] = body.role_name
    save_oauth_token(user_id, "aws-sso", token)

    try:
        secrets_store.delete(oauth_secret_id(user_id, "aws-sso-pending"))
    except Exception:
        pass

    return {"status": "ok", "account_id": body.account_id, "role_name": body.role_name}
