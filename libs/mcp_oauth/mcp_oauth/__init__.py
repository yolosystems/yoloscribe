"""
mcp_oauth — OAuth 2.1 client library for remote MCP servers.

Provides:
  - discover()                    — RFC 8414 / RFC 9728 metadata discovery
  - run_authorization_flow()      — browser-based Authorization Code + PKCE
  - dynamic_client_registration() — RFC 7591 dynamic registration
  - refresh_access_token()        — token refresh
  - TokenStore                    — persistent token storage
  - MCPClient                     — MCP Streamable HTTP client with Bearer auth
"""

from .discovery import discover, AuthorizationServerMetadata, ProtectedResourceMetadata
from .pkce import PKCEChallenge
from .oauth_flow import (
    run_authorization_flow,
    dynamic_client_registration,
    refresh_access_token,
    build_authorization_url,
    OAuthError,
)
from .token_store import TokenStore
from .mcp_client import MCPClient, MCPError

__all__ = [
    "discover",
    "AuthorizationServerMetadata",
    "ProtectedResourceMetadata",
    "PKCEChallenge",
    "run_authorization_flow",
    "dynamic_client_registration",
    "refresh_access_token",
    "build_authorization_url",
    "OAuthError",
    "TokenStore",
    "MCPClient",
    "MCPError",
]
