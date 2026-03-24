"""Abstract base classes for YoloScribe's pluggable auth provider interfaces."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod


@dataclasses.dataclass
class JWTClaims:
    user_id: str
    email: str | None


class AuthProvider(ABC):
    """Handles JWT validation and OAuth PKCE flows."""

    @abstractmethod
    def decode_jwt(self, token: str) -> JWTClaims:
        """Validate a JWT and return user claims. Raises HTTPException on failure."""

    @abstractmethod
    def get_authorize_url(self, redirect_uri: str, code_challenge: str) -> str:
        """Return the provider's authorization URL for a PKCE OAuth flow."""

    @abstractmethod
    async def exchange_code(self, code: str, code_verifier: str) -> dict:
        """Exchange a PKCE authorization code for tokens. Returns the token response dict."""

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> dict:
        """Refresh an access token. Returns the token response dict."""

    @abstractmethod
    def delete_user(self, user_id: str) -> None:
        """Permanently delete a user. Raises HTTPException on failure."""


class UserSiteRepository(ABC):
    """Maps user identity to site name."""

    @abstractmethod
    def get_site_for_user(self, user_id: str) -> str | None:
        """Return the site name for a user, or None if not provisioned."""

    @abstractmethod
    def insert_user_site(self, user_id: str, site_name: str, theme: str) -> None:
        """Create the user→site mapping. Raises HTTPException on failure."""

    @abstractmethod
    def delete_user_site(self, user_id: str) -> None:
        """Remove the user→site mapping. Best-effort; logs but does not raise."""


class ApiTokenRepository(ABC):
    """Stores and validates site-scoped API tokens."""

    @abstractmethod
    def insert_token(
        self,
        user_id: str,
        site_name: str,
        name: str,
        token_hash: str,
        expires_at: str | None = None,
    ) -> str:
        """Insert a new token row and return its UUID. Raises HTTPException on failure."""

    @abstractmethod
    def list_tokens(self, user_id: str) -> list[dict]:
        """Return all non-revoked tokens for a user (without token_hash)."""

    @abstractmethod
    def revoke_token(self, token_id: str, user_id: str) -> bool:
        """Set revoked_at on a token. Returns True if found, False if not."""

    @abstractmethod
    def get_by_hash(self, token_hash: str) -> dict | None:
        """Look up an active (non-revoked) token by hash."""

    @abstractmethod
    def update_last_used(self, token_id: str) -> None:
        """Update last_used_at for a token. Best-effort; never raises."""
