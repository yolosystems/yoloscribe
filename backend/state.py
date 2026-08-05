"""In-memory state for the outbound OAuth (tool enrollment) and AWS SSO flows.

All state dicts are module-level singletons so they are shared across routers
that import from this module.
"""

import dataclasses
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
