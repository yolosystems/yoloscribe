"""RFC 9728 protected-resource metadata for YoloScribe's MCP server.

YoloScribe's MCP is an OAuth *resource server* only — its authorization server is
an external OIDC provider (Supabase today; any OIDC provider under Item 3). This
advertises that AS so MCP clients and the LiteLLM gateway can discover it via the
`WWW-Authenticate: ... resource_metadata=...` challenge. YoloScribe runs no OAuth
authorization server of its own (the internal AS in the old routers/mcp_oauth.py
was deleted in YOL-505); token issuance + login are entirely the OIDC provider's.
"""
from fastapi import APIRouter

from config import MCP_OAUTH_ISSUER, mcp_api_base

router = APIRouter()


def _protected_resource_metadata() -> dict:
    base = mcp_api_base()
    return {
        "resource": f"{base}/mcp/v1",
        "authorization_servers": [MCP_OAUTH_ISSUER] if MCP_OAUTH_ISSUER else [],
    }


@router.get(
    "/.well-known/oauth-protected-resource",
    tags=["mcp"],
    summary="OAuth 2.0 Protected Resource Metadata (RFC 9728)",
)
async def oauth_protected_resource() -> dict:
    """Advertise the external authorization server(s) for the MCP resource."""
    return _protected_resource_metadata()


@router.get(
    "/.well-known/oauth-protected-resource/{resource_path:path}",
    tags=["mcp"],
    summary="OAuth 2.0 Protected Resource Metadata (RFC 9728) — path-scoped probe",
)
async def oauth_protected_resource_scoped(resource_path: str) -> dict:
    """Same metadata for clients that probe the path-suffixed well-known URL."""
    return _protected_resource_metadata()
