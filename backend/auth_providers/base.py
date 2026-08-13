"""Abstract base classes for YoloScribe's pluggable auth provider interfaces."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod


@dataclasses.dataclass
class JWTClaims:
    user_id: str
    email: str | None


class AuthProvider(ABC):
    """Validates externally-issued OIDC JWTs and handles account deletion.

    YoloScribe runs no OAuth authorization server of its own (YOL-505): token
    issuance and login live entirely in the external OIDC provider. This provider
    only *validates* the tokens that provider issued and deletes accounts — the
    old authorize / code-exchange / refresh methods were removed with the internal
    MCP OAuth server.
    """

    @abstractmethod
    def decode_jwt(self, token: str) -> JWTClaims:
        """Validate a JWT and return user claims. Raises HTTPException on failure."""

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
    def get_by_id(self, token_id: str) -> dict | None:
        """Look up an active (non-revoked) token by its row ID.

        Returns at least {id, user_id, site_name, expires_at}, or None if the
        token is unknown or revoked. Used to resolve a messaging-channel binding
        back to its owner, so revoking a token also disconnects its channels.
        """

    @abstractmethod
    def update_last_used(self, token_id: str) -> None:
        """Update last_used_at for a token. Best-effort; never raises."""


class MessagingConfigRepository(ABC):
    """Stores messaging-channel connections (channel → API-token mappings).

    A row binds one platform channel to one API token; the token in turn
    identifies the owning user and site. Deliberately stores no credential —
    resolution goes channel → api_token_id → (user_id, site_name), so the
    messaging bot never handles a user's API token (YOL-523).
    """

    @abstractmethod
    def list_by_token_ids(self, token_ids: list[str]) -> list[dict]:
        """Return configs owned by any of the given API-token IDs.

        Each dict carries: id, platform, connection (dict), created_at, api_token_id.
        """

    @abstractmethod
    def get(self, config_id: str) -> dict | None:
        """Look up a single config by ID. Returns at least {id, api_token_id}, or None."""

    @abstractmethod
    def get_by_channel(self, platform: str, channel_id: str) -> dict | None:
        """Look up a config by platform + channel. Returns {id, api_token_id}, or None."""

    @abstractmethod
    def upsert(self, platform: str, api_token_id: str, connection: dict) -> str:
        """Create or replace the binding for `connection['channel_id']` on `platform`.

        Re-running /setup on an already-linked channel must rebind it rather than
        create a duplicate. Returns the config ID.
        """

    @abstractmethod
    def delete(self, config_id: str) -> None:
        """Delete a config by ID. Raises HTTPException on backend failure."""
