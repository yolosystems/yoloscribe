"""Pluggable auth check for internal backend-to-backend calls (run-token minting).

Phase 1 (this): a static shared-secret header check — network-boundary trust
plus a lightweight internal secret as defense in depth. It's wholly internal
to one deployment (never handed to a third party), so rotating it is just a
redeploy, not a distributed-secret problem.

The later third-party path (RFC 7591 client registration + RFC 8693 token
exchange with private_key_jwt — see projects/yoloscribe/ideas/delegation-token
in the wiki, Phase 4 of the re-architecture plan) only ever swaps this
function's body. The mint endpoint's route, request/response shape, and the
run token's signing/scoping never change.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException

from config import INTERNAL_MINT_SECRET, MESSAGING_BOT_SECRET


def _check(presented: str, expected: str) -> None:
    """Constant-time comparison. An unset expected secret denies everything."""
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Invalid internal auth")


def check_caller(x_internal_auth: str) -> None:
    """Raise HTTPException(403) if the caller is not trusted to mint run tokens."""
    _check(x_internal_auth, INTERNAL_MINT_SECRET)


def check_messaging_bot(x_internal_auth: str) -> None:
    """Raise HTTPException(403) if the caller is not the messaging bot.

    Deliberately a *different* secret from check_caller: the bot processes
    untrusted input from chat platforms and must not be able to reach
    /internal/runs/mint, which accepts an arbitrary site + user_id (YOL-523).
    """
    _check(x_internal_auth, MESSAGING_BOT_SECRET)
