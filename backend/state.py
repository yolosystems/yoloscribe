"""In-memory state for OAuth, AWS SSO, and MCP OAuth flows.

All state dicts are module-level singletons so they are shared across routers
that import from this module.
"""

import base64
import dataclasses
import hashlib
import time


# ── OAuth (skill auth) ─────────────────────────────────────────────────────────

@dataclasses.dataclass
class OAuthPendingState:
    tool_name: str
    user_id: str
    site: str
    server_url: str
    pkce_verifier: str
    client_id: str
    client_secret: str | None
    auth_metadata: dict  # serialized AuthorizationServerMetadata fields
    created_at: float


oauth_pending: dict[str, OAuthPendingState] = {}


def cleanup_oauth_state() -> None:
    cutoff = time.time() - 600
    for k in [k for k, v in oauth_pending.items() if v.created_at < cutoff]:
        del oauth_pending[k]


# ── AWS SSO ────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class AwsSsoPendingState:
    user_id: str
    site: str
    sso_region: str
    sso_start_url: str
    aws_region: str
    client_id: str
    client_secret: str
    device_code: str
    created_at: float
    expires_in: int
    interval: int


aws_sso_pending: dict[str, AwsSsoPendingState] = {}


def cleanup_aws_sso_state() -> None:
    cutoff = time.time()
    for k in [k for k, v in aws_sso_pending.items() if cutoff > v.created_at + v.expires_in]:
        del aws_sso_pending[k]


# ── MCP OAuth ──────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class McpDcrClient:
    """OAuth client registered by Claude Code via Dynamic Client Registration."""
    client_id: str
    redirect_uris: list[str]
    created_at: float


@dataclasses.dataclass
class McpAuthPending:
    """State for an in-flight MCP OAuth authorization request."""
    client_id: str
    cc_code_challenge: str
    cc_code_challenge_method: str
    cc_redirect_uri: str
    cc_state: str | None
    supabase_pkce_verifier: str
    created_at: float


@dataclasses.dataclass
class McpCode:
    """One-time authorization code issued to Claude Code after Supabase callback."""
    client_id: str
    supabase_jwt: str
    supabase_refresh_token: str | None
    cc_code_challenge: str
    cc_code_challenge_method: str
    cc_redirect_uri: str
    cc_state: str | None
    created_at: float


mcp_dcr_clients: dict[str, McpDcrClient] = {}
mcp_auth_pending: dict[str, McpAuthPending] = {}
mcp_codes: dict[str, McpCode] = {}


def cleanup_mcp_state() -> None:
    cutoff_10m = time.time() - 600
    cutoff_5m = time.time() - 300
    for k in [k for k, v in mcp_auth_pending.items() if v.created_at < cutoff_10m]:
        del mcp_auth_pending[k]
    for k in [k for k, v in mcp_codes.items() if v.created_at < cutoff_5m]:
        del mcp_codes[k]


# ── PKCE helper ────────────────────────────────────────────────────────────────

def pkce_s256(verifier: str) -> str:
    """Return the S256 code challenge for the given verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
