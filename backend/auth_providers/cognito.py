"""Cognito + DynamoDB implementations of AuthProvider, UserSiteRepository, and ApiTokenRepository."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
import httpx
import jwt as pyjwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .base import ApiTokenRepository, AuthProvider, JWTClaims, UserSiteRepository

log = logging.getLogger(__name__)

_TTL = 300  # 5-minute site-lookup cache


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

    def get_authorize_url(self, redirect_uri: str, code_challenge: str) -> str:
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
        return f"{self._domain}/oauth2/authorize?{params}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._domain}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "",  # caller must match the redirect_uri used in authorize
                    "code_verifier": code_verifier,
                    "client_id": self._client_id,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    **self._basic_auth_header(),
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def refresh_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._domain}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    **self._basic_auth_header(),
                },
            )
            resp.raise_for_status()
            return resp.json()

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

    def _basic_auth_header(self) -> dict[str, str]:
        creds = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}


class DynamoDBUserSiteRepository(UserSiteRepository):
    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._dynamodb = boto3.client("dynamodb", region_name=region)
        self._cache: dict[str, tuple[str | None, float]] = {}

    def get_site_for_user(self, user_id: str) -> str | None:
        now = time.time()
        if user_id in self._cache:
            site, ts = self._cache[user_id]
            if now - ts < _TTL:
                return site

        try:
            resp = self._dynamodb.get_item(
                TableName=self._table_name,
                Key={"user_id": {"S": user_id}},
                ProjectionExpression="site_name",
            )
            item = resp.get("Item")
            site = item["site_name"]["S"] if item else None
        except Exception:
            site = None

        self._cache[user_id] = (site, now)
        return site

    def insert_user_site(self, user_id: str, site_name: str, theme: str) -> None:
        try:
            self._dynamodb.put_item(
                TableName=self._table_name,
                Item={
                    "user_id": {"S": user_id},
                    "site_name": {"S": site_name},
                    "theme": {"S": theme},
                },
                ConditionExpression="attribute_not_exists(user_id)",
            )
            self._cache[user_id] = (site_name, time.time())
        except self._dynamodb.exceptions.ConditionalCheckFailedException as exc:
            raise HTTPException(status_code=409, detail="User already has a provisioned site") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DynamoDB error: {exc}") from exc

    def delete_user_site(self, user_id: str) -> None:
        try:
            self._dynamodb.delete_item(
                TableName=self._table_name,
                Key={"user_id": {"S": user_id}},
            )
            self._cache.pop(user_id, None)
        except Exception as exc:
            log.warning("Failed to delete user_site row for %s: %s", user_id, exc)


class DynamoDBApiTokenRepository(ApiTokenRepository):
    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._dynamodb = boto3.client("dynamodb", region_name=region)

    def insert_token(
        self,
        user_id: str,
        site_name: str,
        name: str,
        token_hash: str,
        expires_at: str | None = None,
    ) -> str:
        token_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        item: dict = {
            "token_id": {"S": token_id},
            "user_id": {"S": user_id},
            "site_name": {"S": site_name},
            "name": {"S": name},
            "token_hash": {"S": token_hash},
            "created_at": {"S": now},
        }
        if expires_at:
            item["expires_at"] = {"S": expires_at}
        try:
            self._dynamodb.put_item(TableName=self._table_name, Item=item)
            return token_id
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DynamoDB error: {exc}") from exc

    def list_tokens(self, user_id: str) -> list[dict]:
        try:
            resp = self._dynamodb.query(
                TableName=self._table_name,
                IndexName="user_id-index",
                KeyConditionExpression="user_id = :uid",
                FilterExpression="attribute_not_exists(revoked_at)",
                ExpressionAttributeValues={":uid": {"S": user_id}},
                ProjectionExpression="token_id,#n,site_name,created_at,expires_at,last_used_at",
                ExpressionAttributeNames={"#n": "name"},
            )
            return [_unmarshal_token(item) for item in resp.get("Items", [])]
        except Exception:
            return []

    def revoke_token(self, token_id: str, user_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=self._table_name,
                Key={"token_id": {"S": token_id}},
                UpdateExpression="SET revoked_at = :now",
                ConditionExpression="user_id = :uid AND attribute_not_exists(revoked_at)",
                ExpressionAttributeValues={":now": {"S": now}, ":uid": {"S": user_id}},
            )
            return True
        except self._dynamodb.exceptions.ConditionalCheckFailedException:
            return False
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DynamoDB error: {exc}") from exc

    def get_by_hash(self, token_hash: str) -> dict | None:
        try:
            resp = self._dynamodb.query(
                TableName=self._table_name,
                IndexName="token_hash-index",
                KeyConditionExpression="token_hash = :h",
                FilterExpression="attribute_not_exists(revoked_at)",
                ExpressionAttributeValues={":h": {"S": token_hash}},
                ProjectionExpression="token_id,user_id,site_name,expires_at",
                Limit=1,
            )
            items = resp.get("Items", [])
            if not items:
                return None
            item = items[0]
            return {
                "id": item["token_id"]["S"],
                "user_id": item["user_id"]["S"],
                "site_name": item["site_name"]["S"],
                "expires_at": item["expires_at"]["S"] if "expires_at" in item else None,
            }
        except Exception:
            return None

    def update_last_used(self, token_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._dynamodb.update_item(
                TableName=self._table_name,
                Key={"token_id": {"S": token_id}},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={":now": {"S": now}},
            )
        except Exception as exc:
            log.warning("Failed to update token last_used_at for %s: %s", token_id, exc)


def _unmarshal_token(item: dict) -> dict:
    """Convert a DynamoDB item dict to the flat dict expected by TokenListItem."""
    return {
        "id": item["token_id"]["S"],
        "name": item["name"]["S"],
        "site_name": item["site_name"]["S"],
        "created_at": item["created_at"]["S"],
        "expires_at": item.get("expires_at", {}).get("S"),
        "last_used_at": item.get("last_used_at", {}).get("S"),
    }
