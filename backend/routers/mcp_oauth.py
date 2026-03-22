"""MCP OAuth 2.0 Authorization Server endpoints.

Turns YoloScribe into an OAuth AS for Claude Code so users can authenticate
with their Google/Supabase identity.

Flow:
  1. Claude Code discovers the AS via GET /.well-known/oauth-authorization-server
  2. Claude Code registers itself via POST /mcp/oauth/register (DCR, RFC 7591)
  3. Claude Code sends the user to GET /mcp/oauth/authorize — we generate our
     own PKCE pair, store Claude Code's PKCE challenge, and redirect the user
     to Supabase's Google OAuth endpoint with *our* PKCE challenge embedded.
  4. Supabase redirects back to GET /mcp/oauth/callback/{mcp_state}?code=...
     We exchange the code for a Supabase JWT (server-side PKCE), store it as
     a one-time code, and redirect Claude Code to its redirect_uri?code=...
  5. Claude Code calls POST /mcp/oauth/token with the code + its PKCE verifier.
     We verify S256(verifier) == challenge and return the Supabase JWT.

Entirely separate from the existing /oauth/* skill auth flow.
"""

import logging
import secrets
import time
import urllib.parse
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL, mcp_api_base
from state import (
    McpAuthPending,
    McpCode,
    McpDcrClient,
    cleanup_mcp_state,
    mcp_auth_pending,
    mcp_codes,
    mcp_dcr_clients,
    pkce_s256,
)

router = APIRouter()


@router.get(
    "/.well-known/oauth-authorization-server",
    tags=["mcp"],
    summary="OAuth 2.0 Authorization Server Metadata (RFC 8414)",
    include_in_schema=True,
)
async def mcp_oauth_server_metadata() -> dict:
    """Return RFC 8414 OAuth AS metadata so Claude Code can discover our endpoints."""
    base = mcp_api_base()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.post(
    "/mcp/oauth/register",
    tags=["mcp"],
    summary="Dynamic Client Registration (RFC 7591)",
    status_code=201,
)
async def mcp_oauth_register(request: Request) -> dict:
    """Register a new OAuth client (Claude Code) and return a client_id."""
    body = await request.json()
    redirect_uris: list[str] = body.get("redirect_uris", [])
    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")

    client_id = str(uuid.uuid4())
    mcp_dcr_clients[client_id] = McpDcrClient(
        client_id=client_id,
        redirect_uris=redirect_uris,
        created_at=time.time(),
    )
    return {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@router.get(
    "/mcp/oauth/authorize",
    tags=["mcp"],
    summary="OAuth 2.0 authorization endpoint — initiates Google login via Supabase",
)
async def mcp_oauth_authorize(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    state: str | None = Query(default=None),
    scope: str | None = Query(default=None),
) -> RedirectResponse:
    """Validate the authorization request and redirect the user to Supabase/Google."""
    cleanup_mcp_state()

    if response_type != "code":
        raise HTTPException(status_code=400, detail="Only response_type=code is supported")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="Only S256 code_challenge_method is supported")

    parsed_redir = urllib.parse.urlparse(redirect_uri)
    is_loopback = parsed_redir.hostname in ("localhost", "127.0.0.1", "::1")
    registered = mcp_dcr_clients.get(client_id)
    if not is_loopback and (registered is None or redirect_uri not in registered.redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri must be a loopback address or pre-registered via DCR")

    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase is not configured on this server")

    supabase_verifier = secrets.token_urlsafe(48)
    supabase_challenge = pkce_s256(supabase_verifier)

    internal_state = secrets.token_urlsafe(32)
    mcp_auth_pending[internal_state] = McpAuthPending(
        client_id=client_id,
        cc_code_challenge=code_challenge,
        cc_code_challenge_method=code_challenge_method,
        cc_redirect_uri=redirect_uri,
        cc_state=state,
        supabase_pkce_verifier=supabase_verifier,
        created_at=time.time(),
    )

    base = mcp_api_base()
    callback_url = f"{base}/mcp/oauth/callback/{urllib.parse.quote(internal_state, safe='')}"
    supabase_auth_url = (
        f"{SUPABASE_URL}/auth/v1/authorize"
        f"?provider=google"
        f"&code_challenge={urllib.parse.quote(supabase_challenge, safe='')}"
        f"&code_challenge_method=S256"
        f"&redirect_to={urllib.parse.quote(callback_url, safe='')}"
    )
    return RedirectResponse(url=supabase_auth_url, status_code=302)


@router.get(
    "/mcp/oauth/callback/{mcp_state}",
    tags=["mcp"],
    summary="Supabase OAuth callback — exchanges Supabase code for JWT, issues code to client",
)
async def mcp_oauth_callback(
    mcp_state: str,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Receive the authorization code from Supabase and issue a one-time code to Claude Code."""
    cleanup_mcp_state()

    pending = mcp_auth_pending.get(mcp_state)
    if not pending:
        return Response(  # type: ignore[return-value]
            content="Invalid or expired OAuth state. Please restart the authentication flow.",
            status_code=400,
        )

    if error:
        del mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": error, "error_description": error_description or error})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    if not code:
        del mcp_auth_pending[mcp_state]
        return Response(content="Missing code parameter from Supabase.", status_code=400)  # type: ignore[return-value]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                f"{SUPABASE_URL}/auth/v1/token",
                params={"grant_type": "pkce"},
                json={"auth_code": code, "code_verifier": pending.supabase_pkce_verifier},
                headers={"apikey": SUPABASE_SERVICE_ROLE_KEY},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
    except Exception as exc:
        logging.warning("MCP OAuth: Supabase code exchange failed: %s", exc)
        del mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": "server_error", "error_description": "Token exchange failed"})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    supabase_jwt: str = token_data.get("access_token", "")
    supabase_refresh: str | None = token_data.get("refresh_token")

    if not supabase_jwt:
        del mcp_auth_pending[mcp_state]
        error_params = urllib.parse.urlencode({"error": "server_error", "error_description": "No access token returned"})
        return RedirectResponse(url=f"{pending.cc_redirect_uri}?{error_params}", status_code=302)

    del mcp_auth_pending[mcp_state]

    our_code = secrets.token_urlsafe(32)
    mcp_codes[our_code] = McpCode(
        client_id=pending.client_id,
        supabase_jwt=supabase_jwt,
        supabase_refresh_token=supabase_refresh,
        cc_code_challenge=pending.cc_code_challenge,
        cc_code_challenge_method=pending.cc_code_challenge_method,
        cc_redirect_uri=pending.cc_redirect_uri,
        cc_state=pending.cc_state,
        created_at=time.time(),
    )

    params: dict[str, str] = {"code": our_code}
    if pending.cc_state:
        params["state"] = pending.cc_state
    redirect_url = f"{pending.cc_redirect_uri}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post(
    "/mcp/oauth/token",
    tags=["mcp"],
    summary="OAuth 2.0 token endpoint — exchange code or refresh token for Supabase JWT",
)
async def mcp_oauth_token(request: Request) -> dict:
    """Exchange an authorization code (+ PKCE verifier) or refresh token for a Supabase JWT."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type: str = body.get("grant_type", "")

    if grant_type == "authorization_code":
        code: str = body.get("code", "")
        code_verifier: str = body.get("code_verifier", "")

        stored = mcp_codes.get(code)
        if not stored:
            raise HTTPException(status_code=400, detail="Invalid or expired authorization code")

        expected_challenge = pkce_s256(code_verifier)
        if expected_challenge != stored.cc_code_challenge:
            raise HTTPException(status_code=400, detail="PKCE verification failed")

        del mcp_codes[code]

        response: dict = {
            "access_token": stored.supabase_jwt,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if stored.supabase_refresh_token:
            response["refresh_token"] = stored.supabase_refresh_token
        return response

    elif grant_type == "refresh_token":
        refresh_token: str = body.get("refresh_token", "")
        if not refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token is required")
        if not SUPABASE_URL:
            raise HTTPException(status_code=503, detail="Supabase is not configured on this server")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{SUPABASE_URL}/auth/v1/token",
                    params={"grant_type": "refresh_token"},
                    json={"refresh_token": refresh_token},
                    headers={"apikey": SUPABASE_SERVICE_ROLE_KEY},
                )
                resp.raise_for_status()
                token_data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Token refresh failed: {exc}") from exc

        result: dict = {
            "access_token": token_data.get("access_token", ""),
            "token_type": "Bearer",
            "expires_in": token_data.get("expires_in", 3600),
        }
        if token_data.get("refresh_token"):
            result["refresh_token"] = token_data["refresh_token"]
        return result

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported grant_type: {grant_type}")
