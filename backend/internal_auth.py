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

from fastapi import HTTPException

from config import INTERNAL_MINT_SECRET


def check_caller(x_internal_auth: str) -> None:
    """Raise HTTPException(403) if the caller is not trusted to mint run tokens."""
    if not INTERNAL_MINT_SECRET or x_internal_auth != INTERNAL_MINT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid internal auth")
