"""Auth provider factory — reads AUTH_PROVIDER env var and returns implementations."""

from __future__ import annotations

import os

from .base import ApiTokenRepository, AuthProvider, JWTClaims, UserSiteRepository

__all__ = ["AuthProvider", "UserSiteRepository", "ApiTokenRepository", "JWTClaims", "create_providers"]


def create_providers() -> tuple[AuthProvider | None, UserSiteRepository | None, ApiTokenRepository | None]:
    """Instantiate the configured auth provider implementations.

    Returns (None, None, None) when the provider is not fully configured
    (e.g. SUPABASE_URL is unset in local/test environments).
    """
    provider = os.environ.get("AUTH_PROVIDER", "supabase").lower()

    if provider == "supabase":
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not supabase_url:
            return None, None, None
        from .supabase import (  # noqa: PLC0415
            SupabaseApiTokenRepository,
            SupabaseAuthProvider,
            SupabaseUserSiteRepository,
        )
        return (
            SupabaseAuthProvider(supabase_url, supabase_key),
            SupabaseUserSiteRepository(supabase_url, supabase_key),
            SupabaseApiTokenRepository(supabase_url, supabase_key),
        )

    raise ValueError(f"Unknown AUTH_PROVIDER: {provider!r}. Supported values: supabase")
