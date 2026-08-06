"""Cognito implementation of AuthProvider.

The user→site, API-token, and messaging-config repositories that pair with this
provider live in `auth_providers/dynamodb.py` (shared with the generic-OIDC
provider); this module holds only the Cognito-specific JWT validation.
"""

from __future__ import annotations

import logging

import boto3
import jwt as pyjwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .base import AuthProvider, JWTClaims

log = logging.getLogger(__name__)


class CognitoAuthProvider(AuthProvider):
    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        client_secret: str,
        cognito_domain: str,
        region: str,
    ) -> None:
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._domain = cognito_domain.rstrip("/")
        self._region = region
        jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=600)
        self._cognito_client = boto3.client("cognito-idp", region_name=region)

    def decode_jwt(self, token: str) -> JWTClaims:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
            return JWTClaims(user_id=payload["sub"], email=payload.get("email"))
        except pyjwt.exceptions.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    def delete_user(self, user_id: str) -> None:
        try:
            self._cognito_client.admin_delete_user(
                UserPoolId=self._user_pool_id,
                Username=user_id,
            )
        except self._cognito_client.exceptions.UserNotFoundException:
            pass  # already deleted — treat as success
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Cognito delete error: {exc}") from exc
