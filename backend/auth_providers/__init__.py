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

    if provider == "cognito":
        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
        client_id = os.environ.get("COGNITO_CLIENT_ID", "")
        client_secret = os.environ.get("COGNITO_CLIENT_SECRET", "")
        cognito_domain = os.environ.get("COGNITO_DOMAIN", "")
        region = os.environ.get("AWS_REGION", "us-east-1")
        user_site_table = os.environ.get("DYNAMODB_USER_SITE_TABLE", "yoloscribe-user-site")
        api_tokens_table = os.environ.get("DYNAMODB_API_TOKENS_TABLE", "yoloscribe-api-tokens")
        if not user_pool_id or not client_id or not cognito_domain:
            return None, None, None
        from .cognito import (  # noqa: PLC0415
            CognitoAuthProvider,
            DynamoDBApiTokenRepository,
            DynamoDBUserSiteRepository,
        )
        return (
            CognitoAuthProvider(user_pool_id, client_id, client_secret, cognito_domain, region),
            DynamoDBUserSiteRepository(user_site_table, region),
            DynamoDBApiTokenRepository(api_tokens_table, region),
        )

    raise ValueError(f"Unknown AUTH_PROVIDER: {provider!r}. Supported values: supabase, cognito")
