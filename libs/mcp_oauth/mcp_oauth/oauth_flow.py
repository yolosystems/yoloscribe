"""
OAuth 2.1 Authorization Code Flow with PKCE.

Handles:
  - Dynamic client registration (RFC 7591)
  - Authorization URL construction
  - Local callback HTTP server to capture the authorization code
  - Authorization code → token exchange
  - Token refresh
"""

import asyncio
import secrets
import webbrowser
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from .discovery import AuthorizationServerMetadata
from .pkce import PKCEChallenge


class OAuthError(Exception):
    pass


async def dynamic_client_registration(
    client: httpx.AsyncClient,
    registration_endpoint: str,
    redirect_uri: str,
    client_name: str = "MCP OAuth Test Client",
) -> dict:
    """
    RFC 7591: OAuth 2.0 Dynamic Client Registration.
    Registers a public native client and returns the client metadata.
    """
    response = await client.post(
        registration_endpoint,
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # public client
            "application_type": "native",
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code not in (200, 201):
        raise OAuthError(
            f"Dynamic client registration failed: {response.status_code} {response.text}"
        )
    return response.json()


def build_authorization_url(
    metadata: AuthorizationServerMetadata,
    client_id: str,
    redirect_uri: str,
    pkce: PKCEChallenge,
    state: str,
    scopes: list[str],
    resource: Optional[str] = None,
) -> str:
    """Build the OAuth authorization URL with PKCE parameters.

    *resource* is the RFC 8707 resource indicator — the URL of the protected
    resource (i.e. the MCP server).  Required by authorization servers that
    issue tokens for multiple resource servers (e.g. Linear).
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.challenge_method,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if resource:
        params["resource"] = resource
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


async def _run_callback_server(port: int, expected_state: str) -> tuple[str, str]:
    """
    Spin up a local HTTP server on 127.0.0.1:{port} that captures a single
    OAuth redirect and returns (code, state).  Times out after 5 minutes.
    """
    loop = asyncio.get_event_loop()
    result: asyncio.Future[tuple[str, str]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("utf-8", errors="replace").strip()
            # Drain headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            parts = request_line.split(" ")
            if len(parts) < 2:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nBad Request")
                await writer.drain()
                writer.close()
                return

            path = parts[1]
            qs = parse_qs(urlparse(path).query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
            error = qs.get("error", [None])[0]

            if error:
                desc = qs.get("error_description", [""])[0]
                body = f"<h1>Error</h1><p>{error}: {desc}</p><p>You may close this window.</p>"
                writer.write(
                    f"HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\n\r\n{body}".encode()
                )
                await writer.drain()
                writer.close()
                if not result.done():
                    result.set_exception(OAuthError(f"Authorization error: {error}: {desc}"))
                return

            if code and state:
                body = "<h1>Authorization successful!</h1><p>You may close this window.</p>"
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n{body}".encode()
                )
                await writer.drain()
                writer.close()
                if not result.done():
                    result.set_result((code, state))
            else:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing code or state")
                await writer.drain()
                writer.close()
        except Exception as exc:
            if not result.done():
                result.set_exception(exc)

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        async with server:
            code, state = await asyncio.wait_for(result, timeout=300.0)
    finally:
        server.close()

    if state != expected_state:
        raise OAuthError(f"State mismatch: expected {expected_state!r}, got {state!r}")

    return code, state


async def exchange_code(
    metadata: AuthorizationServerMetadata,
    code: str,
    redirect_uri: str,
    client_id: str,
    pkce: PKCEChallenge,
    client_secret: Optional[str] = None,
) -> dict:
    """Exchange an authorization code for tokens."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": pkce.verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            metadata.token_endpoint,
            data=data,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        raise OAuthError(f"Token exchange failed: {response.status_code} {response.text}")
    return response.json()


async def refresh_access_token(
    metadata: AuthorizationServerMetadata,
    refresh_token: str,
    client_id: str,
    client_secret: Optional[str] = None,
) -> dict:
    """Use a refresh token to obtain a new access token."""
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            metadata.token_endpoint,
            data=data,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        raise OAuthError(f"Token refresh failed: {response.status_code} {response.text}")
    return response.json()


async def run_authorization_flow(
    metadata: AuthorizationServerMetadata,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    client_secret: Optional[str] = None,
    callback_port: int = 8787,
    resource: Optional[str] = None,
) -> dict:
    """
    Run the full OAuth 2.1 Authorization Code + PKCE flow interactively.

    Opens the system browser, waits for the local callback, exchanges the
    code for tokens, and returns the token response dict.

    *resource* is the RFC 8707 resource indicator (MCP server URL).
    """
    pkce = PKCEChallenge()
    state = secrets.token_urlsafe(16)

    auth_url = build_authorization_url(
        metadata=metadata,
        client_id=client_id,
        redirect_uri=redirect_uri,
        pkce=pkce,
        state=state,
        scopes=scopes,
        resource=resource,
    )

    print(f"\nOpening browser for authorization...")
    print(f"If the browser does not open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for callback on http://localhost:{callback_port}/callback ...")
    code, _ = await _run_callback_server(callback_port, state)
    print("Authorization code received.")

    print("Exchanging authorization code for tokens...")
    tokens = await exchange_code(
        metadata=metadata,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        pkce=pkce,
        client_secret=client_secret,
    )
    return tokens
