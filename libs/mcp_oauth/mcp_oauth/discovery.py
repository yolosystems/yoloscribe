"""
OAuth 2.1 discovery for MCP servers.

Implements:
  - RFC 9728: OAuth 2.0 Protected Resource Metadata
  - RFC 8414: OAuth 2.0 Authorization Server Metadata
  - MCP spec: probe server for 401 + WWW-Authenticate resource_metadata hint
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx


@dataclass
class AuthorizationServerMetadata:
    """RFC 8414 Authorization Server Metadata."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str] = None
    scopes_supported: list[str] = field(default_factory=list)
    code_challenge_methods_supported: list[str] = field(default_factory=list)
    response_types_supported: list[str] = field(default_factory=list)
    token_endpoint_auth_methods_supported: list[str] = field(default_factory=list)


@dataclass
class ProtectedResourceMetadata:
    """RFC 9728 Protected Resource Metadata."""

    resource: str
    authorization_servers: list[str] = field(default_factory=list)
    bearer_methods_supported: list[str] = field(default_factory=list)
    scopes_supported: list[str] = field(default_factory=list)


def _parse_www_authenticate(header: str) -> dict[str, str]:
    """Extract key="value" pairs from a WWW-Authenticate header."""
    return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)="([^"]*)"', header)}


async def _fetch_as_metadata(
    client: httpx.AsyncClient, issuer_url: str
) -> Optional[AuthorizationServerMetadata]:
    """
    Fetch authorization server metadata per RFC 8414.
    Tries /.well-known/oauth-authorization-server{path} at the issuer origin.
    """
    parsed = urlparse(issuer_url.rstrip("/"))
    path = parsed.path or ""
    well_known = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{path}"

    try:
        response = await client.get(well_known, follow_redirects=True)
        if response.status_code == 200:
            data = response.json()
            return AuthorizationServerMetadata(
                issuer=data["issuer"],
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                registration_endpoint=data.get("registration_endpoint"),
                scopes_supported=data.get("scopes_supported", []),
                code_challenge_methods_supported=data.get("code_challenge_methods_supported", []),
                response_types_supported=data.get("response_types_supported", []),
                token_endpoint_auth_methods_supported=data.get(
                    "token_endpoint_auth_methods_supported", []
                ),
            )
    except (httpx.RequestError, KeyError, ValueError):
        pass
    return None


async def discover(
    server_url: str,
) -> tuple[Optional[ProtectedResourceMetadata], Optional[AuthorizationServerMetadata]]:
    """
    Discover OAuth metadata for an MCP server.

    Discovery order:
      1. POST to MCP endpoint, inspect 401 WWW-Authenticate for resource_metadata URL
      2. GET /.well-known/oauth-protected-resource at the server origin
      3. GET /.well-known/oauth-authorization-server at the server origin

    Returns (ProtectedResourceMetadata | None, AuthorizationServerMetadata | None).
    """
    parsed = urlparse(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=15.0) as client:

        # --- Step 1: Probe MCP endpoint for 401 + WWW-Authenticate ---
        try:
            probe = await client.post(
                server_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-oauth-discovery", "version": "1.0.0"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            if probe.status_code == 401:
                www_auth = probe.headers.get("www-authenticate", "")
                params = _parse_www_authenticate(www_auth)
                resource_metadata_url = params.get("resource_metadata")
                if resource_metadata_url:
                    prm_resp = await client.get(resource_metadata_url, follow_redirects=True)
                    if prm_resp.status_code == 200:
                        prm_data = prm_resp.json()
                        prm = ProtectedResourceMetadata(
                            resource=prm_data.get("resource", server_url),
                            authorization_servers=prm_data.get("authorization_servers", []),
                            bearer_methods_supported=prm_data.get(
                                "bearer_methods_supported", ["header"]
                            ),
                            scopes_supported=prm_data.get("scopes_supported", []),
                        )
                        if prm.authorization_servers:
                            asm = await _fetch_as_metadata(client, prm.authorization_servers[0])
                            return prm, asm
                        return prm, None
        except (httpx.RequestError, ValueError):
            pass

        # --- Step 2: Try protected resource metadata at origin ---
        try:
            prm_resp = await client.get(
                f"{origin}/.well-known/oauth-protected-resource", follow_redirects=True
            )
            if prm_resp.status_code == 200:
                prm_data = prm_resp.json()
                prm = ProtectedResourceMetadata(
                    resource=prm_data.get("resource", server_url),
                    authorization_servers=prm_data.get("authorization_servers", []),
                    bearer_methods_supported=prm_data.get("bearer_methods_supported", ["header"]),
                    scopes_supported=prm_data.get("scopes_supported", []),
                )
                if prm.authorization_servers:
                    asm = await _fetch_as_metadata(client, prm.authorization_servers[0])
                    return prm, asm
                return prm, None
        except (httpx.RequestError, ValueError):
            pass

        # --- Step 3: Try authorization server metadata directly at origin ---
        try:
            asm = await _fetch_as_metadata(client, origin)
            if asm:
                return None, asm
        except (httpx.RequestError, ValueError):
            pass

    return None, None
