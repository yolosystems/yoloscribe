"""LiteLLM admin API — mint / delete per-user virtual keys (YOL-513).

Each user gets one budgeted virtual key: the proxy enforces the budget and
returns 429 when exhausted, so YoloScribe no longer tracks token usage itself.
Minting uses the proxy's master key; the minted key is stored per-user in
Secrets Manager (see credentials.py) and presented as the inference key.

Best-effort: when the proxy is unconfigured/unreachable, mint returns None and
the caller falls back to the shared key (local dev, or before a proxy exists).
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

# Budget applied to each user's key, per budget_duration window. Override via env.
DEFAULT_MAX_BUDGET = float(os.getenv("LITELLM_USER_MAX_BUDGET", "5"))
DEFAULT_BUDGET_DURATION = os.getenv("LITELLM_USER_BUDGET_DURATION", "1d")


def _admin() -> tuple[str, str]:
    """Return (proxy_base_url_without_/v1, admin_key)."""
    base = os.getenv("LITELLM_BASE_URL", "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    # Admin ops use the master key; fall back to LITELLM_API_KEY (== master in the
    # shared-key setup) so local dev works without a separate var.
    admin_key = os.getenv("LITELLM_MASTER_KEY", "").strip() or os.getenv("LITELLM_API_KEY", "").strip()
    return base, admin_key


def mint_user_key(
    user_id: str,
    *,
    max_budget: float | None = None,
    budget_duration: str | None = None,
) -> str | None:
    """Mint a budgeted virtual key for a user. Returns the key, or None if the
    proxy is unconfigured/unreachable (best-effort — the caller falls back)."""
    base, admin_key = _admin()
    if not base or not admin_key:
        log.info("LiteLLM not configured — skipping virtual-key mint for %s", user_id)
        return None
    try:
        resp = httpx.post(
            f"{base}/key/generate",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "user_id": user_id,
                "max_budget": DEFAULT_MAX_BUDGET if max_budget is None else max_budget,
                "budget_duration": budget_duration or DEFAULT_BUDGET_DURATION,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()["key"]
    except Exception as exc:
        log.warning("LiteLLM key mint failed for %s: %s", user_id, exc)
        return None


def key_info(key: str) -> dict | None:
    """Return a virtual key's LiteLLM metadata (spend, max_budget, budget_reset_at,
    …), or None if unavailable. Uses the master key to query by key value."""
    if not key:
        return None
    base, admin_key = _admin()
    if not base or not admin_key:
        return None
    try:
        resp = httpx.get(
            f"{base}/key/info",
            params={"key": key},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        info = data.get("info", data) if isinstance(data, dict) else {}
        return info if isinstance(info, dict) else None
    except Exception as exc:
        log.warning("LiteLLM key_info failed: %s", exc)
        return None


def delete_user_key(key: str) -> None:
    """Delete a virtual key from the proxy (best-effort; never raises)."""
    if not key:
        return
    base, admin_key = _admin()
    if not base or not admin_key:
        return
    try:
        httpx.post(
            f"{base}/key/delete",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"keys": [key]},
            timeout=15.0,
        ).raise_for_status()
    except Exception as exc:
        log.warning("LiteLLM key delete failed: %s", exc)
