"""Per-user daily token budget tracking and enforcement via Supabase PostgREST."""

from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT: int = int(os.environ.get("TOKEN_BUDGET_DEFAULT_DAILY_LIMIT", "500000"))


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _resets_at_utc() -> str:
    """ISO-8601 timestamp of the next UTC midnight."""
    tomorrow = datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1)
    return datetime.datetime(
        tomorrow.year, tomorrow.month, tomorrow.day,
        tzinfo=datetime.timezone.utc,
    ).isoformat()


class TokenBudgetRepository:
    """Supabase PostgREST client for token budget data.

    Required schema:
      token_budgets(user_id UUID PK, daily_limit INT)
      token_usage(user_id UUID, usage_date DATE, total_tokens INT; PK=(user_id, usage_date))
      rpc/increment_token_usage(p_user_id UUID, p_date DATE, p_tokens INT)
    """

    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self._url = supabase_url.rstrip("/")
        self._key = supabase_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "apikey": self._key,
            "Content-Type": "application/json",
        }

    def get_limit(self, user_id: str) -> int:
        """Return the daily token limit for this user (per-user override or global default)."""
        qs = urllib.parse.urlencode({
            "user_id": f"eq.{user_id}",
            "select": "daily_limit",
            "limit": "1",
        })
        req = urllib.request.Request(
            f"{self._url}/rest/v1/token_budgets?{qs}",
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                rows = json.loads(resp.read())
                return int(rows[0]["daily_limit"]) if rows else DEFAULT_DAILY_LIMIT
        except Exception as exc:
            log.warning("Failed to fetch token limit for %s: %s", user_id, exc)
            return DEFAULT_DAILY_LIMIT

    def get_used(self, user_id: str) -> int:
        """Return tokens consumed today (UTC) by this user."""
        qs = urllib.parse.urlencode({
            "user_id": f"eq.{user_id}",
            "usage_date": f"eq.{_today_utc()}",
            "select": "total_tokens",
            "limit": "1",
        })
        req = urllib.request.Request(
            f"{self._url}/rest/v1/token_usage?{qs}",
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                rows = json.loads(resp.read())
                return int(rows[0]["total_tokens"]) if rows else 0
        except Exception as exc:
            log.warning("Failed to fetch token usage for %s: %s", user_id, exc)
            return 0

    def record_usage(self, user_id: str, tokens: int) -> None:
        """Atomically add tokens to today's usage total via a Postgres RPC function."""
        if tokens <= 0:
            return
        data = json.dumps({
            "p_user_id": user_id,
            "p_date": _today_utc(),
            "p_tokens": tokens,
        }).encode()
        req = urllib.request.Request(
            f"{self._url}/rest/v1/rpc/increment_token_usage",
            data=data,
            method="POST",
            headers=self._headers(),
        )
        try:
            urllib.request.urlopen(req)
        except Exception as exc:
            log.warning("Failed to record token usage for %s: %s", user_id, exc)
