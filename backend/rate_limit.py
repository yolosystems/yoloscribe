"""Rate-limiting infrastructure for the AgentScribe API.

Uses slowapi (built on the `limits` library) with a per-identity key function.

Key bucketing strategy:
- JWT-authenticated requests  → keyed on the `sub` claim (user UUID).
- API token–authenticated requests → keyed on the token ID once that feature
  is implemented. For now, falls through to IP-based limiting.
- Unauthenticated / malformed requests → keyed on client IP.

The JWT payload is decoded without cryptographic verification here, which is
intentional: rate limiting is a best-effort mechanism and doesn't need to trust
the claim for security purposes.  The route's auth dependency still performs
full JWT verification before any business logic runs.
"""

from __future__ import annotations

import base64
import json
import logging

from slowapi import Limiter
from starlette.requests import Request

from config import REDIS_URL

log = logging.getLogger(__name__)


def _rate_limit_key(request: Request) -> str:
    """Return a rate-limit bucket key for this request.

    Priority:
    1. JWT Bearer token present → decode payload (no verification) → use `sub`.
    2. (Future) API token header → use token ID.
    3. Fall back to client IP (X-Forwarded-For, then direct socket).
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
        try:
            # JWT is three base64url-encoded segments separated by dots.
            parts = token.split(".")
            if len(parts) == 3:
                # Pad to a multiple of 4 for standard base64 decoding.
                padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
                payload = json.loads(base64.b64decode(padded))
                if sub := payload.get("sub"):
                    return f"user:{sub}"
        except Exception:
            pass  # Fall through to IP-based key

    # X-Forwarded-For is set by the ALB; use the first (leftmost) address.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return f"ip:{forwarded_for.split(',')[0].strip()}"

    return f"ip:{request.client.host if request.client else 'unknown'}"


# ── Limiter instance ───────────────────────────────────────────────────────────
# Shared across main.py (middleware) and all router modules (decorators).

if REDIS_URL:
    log.info("Rate limiter using Redis backend: %s", REDIS_URL)
    limiter = Limiter(key_func=_rate_limit_key, storage_uri=REDIS_URL)
else:
    log.info("Rate limiter using in-process memory backend")
    limiter = Limiter(key_func=_rate_limit_key)
