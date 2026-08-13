"""Auth provider factory — reads AUTH_PROVIDER env var and returns implementations."""

from __future__ import annotations

import os

from .base import (
    ApiTokenRepository,
    AuthProvider,
    JWTClaims,
    MessagingConfigRepository,
    UserSiteRepository,
)

__all__ = [
    "AuthProvider",
    "UserSiteRepository",
    "ApiTokenRepository",
    "MessagingConfigRepository",
    "JWTClaims",
    "create_providers",
]

# What create_providers() yields: (auth provider, user→site repo, API-token repo,
# messaging-config repo). Any element is None when the provider is not configured.
Providers = tuple[
    "AuthProvider | None",
    "UserSiteRepository | None",
    "ApiTokenRepository | None",
    "MessagingConfigRepository | None",
]


def create_providers() -> Providers:
    """Instantiate the configured auth provider implementations.

    Returns all-None when the provider is not fully configured (e.g. SUPABASE_URL
    or OIDC_CONFIG_URL is unset in local/test environments).
    """
    provider = os.environ.get("AUTH_PROVIDER", "supabase").lower()

    if provider == "supabase":
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not supabase_url:
            return None, None, None, None
        from .supabase import (  # noqa: PLC0415
            SupabaseApiTokenRepository,
            SupabaseAuthProvider,
            SupabaseMessagingConfigRepository,
            SupabaseUserSiteRepository,
        )
        return (
            SupabaseAuthProvider(supabase_url, supabase_key),
            SupabaseUserSiteRepository(supabase_url, supabase_key),
            SupabaseApiTokenRepository(supabase_url, supabase_key),
            SupabaseMessagingConfigRepository(supabase_url, supabase_key),
        )

    if provider == "cognito":
        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
        client_id = os.environ.get("COGNITO_CLIENT_ID", "")
        cognito_domain = os.environ.get("COGNITO_DOMAIN", "")
        region = os.environ.get("AWS_REGION", "us-east-1")
        if not user_pool_id or not client_id or not cognito_domain:
            return None, None, None, None
        from .cognito import CognitoAuthProvider  # noqa: PLC0415
        return (CognitoAuthProvider(user_pool_id, client_id, cognito_domain, region), *_dynamodb_repos(region))

    if provider == "oidc":
        config_url = os.environ.get("OIDC_CONFIG_URL", "")
        client_id = os.environ.get("OIDC_CLIENT_ID", "")
        audience = os.environ.get("OIDC_AUDIENCE", "")
        issuer = os.environ.get("OIDC_ISSUER", "")
        region = os.environ.get("AWS_REGION", "us-east-1")
        if not config_url:
            return None, None, None, None
        from .oidc import OidcAuthProvider  # noqa: PLC0415
        return (OidcAuthProvider(config_url, client_id, audience, issuer), *_dynamodb_repos(region))

    raise ValueError(f"Unknown AUTH_PROVIDER: {provider!r}. Supported values: supabase, cognito, oidc")


def _dynamodb_repos(
    region: str,
) -> tuple[UserSiteRepository, ApiTokenRepository, MessagingConfigRepository]:
    """Construct the DynamoDB repositories shared by the cognito and oidc providers."""
    user_site_table = os.environ.get("DYNAMODB_USER_SITE_TABLE", "yoloscribe-user-site")
    api_tokens_table = os.environ.get("DYNAMODB_API_TOKENS_TABLE", "yoloscribe-api-tokens")
    messaging_table = os.environ.get("DYNAMODB_MESSAGING_CONFIGS_TABLE", "yoloscribe-messaging-configs")
    from .dynamodb import (  # noqa: PLC0415
        DynamoDBApiTokenRepository,
        DynamoDBMessagingConfigRepository,
        DynamoDBUserSiteRepository,
    )
    return (
        DynamoDBUserSiteRepository(user_site_table, region),
        DynamoDBApiTokenRepository(api_tokens_table, region),
        DynamoDBMessagingConfigRepository(messaging_table, region),
    )
