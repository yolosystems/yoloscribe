"""DynamoDB-backed repositories, shared by the Cognito and generic-OIDC providers.

These implementations store YoloScribe's user→site mapping, API tokens, and
messaging-channel configs in DynamoDB — the storage layer for any deployment
whose IdP is *not* Supabase (Item 3). They carry no auth-provider coupling, so
both `auth_providers/cognito.py` and `auth_providers/oidc.py` construct them.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import HTTPException

from .base import ApiTokenRepository, MessagingConfigRepository, UserSiteRepository

log = logging.getLogger(__name__)

_TTL = 300  # 5-minute site-lookup cache


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

    def get_by_id(self, token_id: str) -> dict | None:
        try:
            resp = self._dynamodb.get_item(
                TableName=self._table_name,
                Key={"token_id": {"S": token_id}},
                ProjectionExpression="token_id,user_id,site_name,expires_at,revoked_at",
            )
        except Exception:
            return None
        item = resp.get("Item")
        # A revoked token resolves to nothing, so revoking it also disconnects
        # any messaging channels bound to it.
        if not item or "revoked_at" in item:
            return None
        return {
            "id": item["token_id"]["S"],
            "user_id": item["user_id"]["S"],
            "site_name": item["site_name"]["S"],
            "expires_at": item["expires_at"]["S"] if "expires_at" in item else None,
        }

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


class DynamoDBMessagingConfigRepository(MessagingConfigRepository):
    """DynamoDB store for messaging-channel configs.

    Rows are written by the backend's internal messaging endpoints (YOL-523); the
    bot itself has no database access. Table `{name}` uses `id` (S) as the primary
    key, with two GSIs: `api_token_id-index` (list a site's configs) and
    `platform_channel-index` (resolve an inbound message to its owner).

    `platform_channel` is a derived attribute, "{platform}:{channel_id}", because
    DynamoDB cannot index into the nested `connection` map. It is written on every
    upsert and must stay consistent with `connection['channel_id']`.

    The `connection` attribute is stored as a JSON string and parsed on read.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._dynamodb = boto3.client("dynamodb", region_name=region)

    def list_by_token_ids(self, token_ids: list[str]) -> list[dict]:
        rows: list[dict] = []
        for token_id in token_ids:
            try:
                resp = self._dynamodb.query(
                    TableName=self._table_name,
                    IndexName="api_token_id-index",
                    KeyConditionExpression="api_token_id = :tid",
                    ExpressionAttributeValues={":tid": {"S": token_id}},
                    ProjectionExpression="id,platform,connection,created_at,api_token_id",
                )
            except Exception:
                continue
            rows.extend(_unmarshal_messaging_config(item) for item in resp.get("Items", []))
        return rows

    def get(self, config_id: str) -> dict | None:
        try:
            resp = self._dynamodb.get_item(
                TableName=self._table_name,
                Key={"id": {"S": config_id}},
                ProjectionExpression="id,api_token_id",
            )
        except Exception:
            return None
        item = resp.get("Item")
        if not item:
            return None
        return {"id": item["id"]["S"], "api_token_id": item["api_token_id"]["S"]}

    def get_by_channel(self, platform: str, channel_id: str) -> dict | None:
        try:
            resp = self._dynamodb.query(
                TableName=self._table_name,
                IndexName="platform_channel-index",
                KeyConditionExpression="platform_channel = :pc",
                ExpressionAttributeValues={":pc": {"S": f"{platform}:{channel_id}"}},
                ProjectionExpression="id,api_token_id",
                Limit=1,
            )
        except Exception:
            return None
        items = resp.get("Items", [])
        if not items:
            return None
        return {"id": items[0]["id"]["S"], "api_token_id": items[0]["api_token_id"]["S"]}

    def upsert(self, platform: str, api_token_id: str, connection: dict) -> str:
        channel_id = str(connection.get("channel_id", ""))
        if not channel_id:
            raise HTTPException(status_code=400, detail="connection.channel_id is required")

        # Rebind rather than duplicate: reuse the existing row's id when this
        # channel is already linked, so re-running /setup re-points it.
        existing = self.get_by_channel(platform, channel_id)
        config_id = existing["id"] if existing else str(uuid.uuid4())

        item = {
            "id": {"S": config_id},
            "platform": {"S": platform},
            "platform_channel": {"S": f"{platform}:{channel_id}"},
            "api_token_id": {"S": api_token_id},
            "connection": {"S": json.dumps(connection)},
            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
        }
        try:
            self._dynamodb.put_item(TableName=self._table_name, Item=item)
            return config_id
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DynamoDB error: {exc}") from exc

    def delete(self, config_id: str) -> None:
        try:
            self._dynamodb.delete_item(
                TableName=self._table_name,
                Key={"id": {"S": config_id}},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DynamoDB error: {exc}") from exc


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


def _unmarshal_messaging_config(item: dict) -> dict:
    """Convert a DynamoDB messaging_configs item to the shape messaging.py expects."""
    raw_connection = item.get("connection", {}).get("S", "{}")
    try:
        connection = json.loads(raw_connection)
    except (ValueError, TypeError):
        connection = {}
    return {
        "id": item["id"]["S"],
        "platform": item.get("platform", {}).get("S", ""),
        "connection": connection,
        "created_at": item.get("created_at", {}).get("S", ""),
        "api_token_id": item.get("api_token_id", {}).get("S", ""),
    }
