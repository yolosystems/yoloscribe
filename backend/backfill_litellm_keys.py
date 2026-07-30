"""Backfill LiteLLM virtual keys for existing users (YOL-513, one-off).

Users provisioned before YOL-513 shipped have no per-user LiteLLM virtual key, so
their inference falls back to the shared key and their budget shows empty
({"used": 0, "limit": null}). This mints and stores a budgeted key for each
existing user that lacks one. Idempotent — users who already have a key are
skipped. New users get a key at /provision, so this only needs to run once after
the YOL-513 rollout.

Reuses the real mint/store path (litellm_keys.mint_user_key + credentials), so it
needs the same env the backend runs with: Secrets Manager access,
LITELLM_BASE_URL / LITELLM_MASTER_KEY (or LITELLM_API_KEY = master), and Supabase.

In-cluster (recommended — the backend pod already has all of it):

    kubectl exec deploy/yoloscribe-backend -n yolo -- python backfill_litellm_keys.py

Locally (real dev Secrets Manager + a reachable proxy; port-forward the litellm
service to localhost:4000 first):

    LOCAL_MODE=false AWS_PROFILE=runyolo_admin LITELLM_BASE_URL=http://localhost:4000/v1 \
      uv run --env-file ../.env python backfill_litellm_keys.py
"""

from __future__ import annotations

import os

import httpx

from credentials import load_litellm_key, save_litellm_key
from litellm_keys import mint_user_key


def _existing_user_ids() -> list[str]:
    """Every user id from the Supabase user_site table (column: user_uuid)."""
    provider = os.environ.get("AUTH_PROVIDER", "supabase").lower()
    if provider != "supabase":
        raise SystemExit(
            f"This backfill enumerates the Supabase user_site table; "
            f"AUTH_PROVIDER={provider!r} is not supported — enumerate that store instead."
        )
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = httpx.get(
        f"{url}/rest/v1/user_site?select=user_uuid",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return [row["user_uuid"] for row in resp.json() if row.get("user_uuid")]


def main() -> None:
    user_ids = _existing_user_ids()
    print(f"Found {len(user_ids)} user(s) in user_site.\n")

    minted = skipped = failed = 0
    for uid in user_ids:
        if load_litellm_key(uid):
            skipped += 1
            print(f"  skip    {uid}  (already has a key)")
            continue
        key = mint_user_key(uid)
        if key:
            save_litellm_key(uid, key)
            minted += 1
            print(f"  minted  {uid}")
        else:
            failed += 1
            print(f"  FAILED  {uid}  (mint returned nothing — check LITELLM_BASE_URL / master key)")

    print(f"\nDone: {minted} minted, {skipped} skipped, {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
