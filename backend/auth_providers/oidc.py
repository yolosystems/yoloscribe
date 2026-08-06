"""Generic OIDC implementation of AuthProvider.

Validates JWTs issued by *any* OIDC-compatible provider (Auth0, Keycloak, Okta,
Cognito, Supabase, …). The provider is configured with the standard OIDC
discovery URL (`{issuer}/.well-known/openid-configuration`); the signing keys
(`jwks_uri`) and `issuer` are read from that document. Pairs with the DynamoDB
repositories in `auth_providers/dynamodb.py`.

YoloScribe is a pure OAuth *resource server* (Item 2 / YOL-505): it only
validates tokens the external IdP issued. There is no standard OIDC admin API
for deleting a user, so `delete_user` is a best-effort no-op — YoloScribe-side
data (site, DynamoDB rows, S3) is still removed by the account-delete flow.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import jwt as pyjwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .base import AuthProvider, JWTClaims

log = logging.getLogger(__name__)


class OidcAuthProvider(AuthProvider):
    def __init__(
        self,
        config_url: str,
        client_id: str = "",
        audience: str = "",
        issuer: str = "",
    ) -> None:
        self._config_url = config_url
        self._client_id = client_id
        # Audience to enforce: an explicit API audience, else the client_id (the
        # id-token audience most providers set), else None → skip aud verification.
        self._audience = audience or client_id or None
        # Issuer may be given explicitly; otherwise it is read from discovery.
        self._issuer = issuer
        # Discovery is lazy (config.py builds providers at import time, so startup
        # must not depend on the IdP being reachable).
        self._jwks: PyJWKClient | None = None
        self._discovered = False

    def _discover(self) -> None:
        """Fetch the OIDC discovery document once; memoize jwks_uri + issuer."""
        if self._discovered:
            return
        req = urllib.request.Request(self._config_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read())
        jwks_uri = doc["jwks_uri"]
        if not self._issuer:
            self._issuer = doc.get("issuer", "")
        self._jwks = PyJWKClient(jwks_uri, cache_keys=True, lifespan=600)
        self._discovered = True

    def decode_jwt(self, token: str) -> JWTClaims:
        try:
            self._discover()
            assert self._jwks is not None  # set by _discover()
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._audience,
                issuer=self._issuer or None,
                options={"verify_aud": self._audience is not None},
            )
            return JWTClaims(user_id=payload["sub"], email=payload.get("email"))
        except pyjwt.exceptions.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            # Discovery failed / malformed — surface as an auth failure, not a 500.
            raise HTTPException(status_code=401, detail=f"OIDC discovery error: {exc}") from exc

    def delete_user(self, user_id: str) -> None:
        # No standard OIDC admin API — leave the external identity for the operator.
        log.warning(
            "OidcAuthProvider.delete_user is a no-op; the identity for %s remains in "
            "the external IdP and must be removed there by the operator.",
            user_id,
        )
