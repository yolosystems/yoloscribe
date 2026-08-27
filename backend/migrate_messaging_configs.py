#!/usr/bin/env python
"""One-off: purge stored API tokens from messaging_configs (YOL-523).

This is an in-place cleanup of the SUPABASE table. Despite living next to
migrate_supabase_to_dynamodb.py, it does NOT move anything to DynamoDB — see
that script's docstring for why messaging_configs is deliberately not migrated
on an auth-provider cutover (regenerated tokens get new UUIDs, so migrated
bindings would all be dead; users re-run /setup instead).

What this does: existing rows already carry everything resolution needs —
`api_token_id` and `connection.channel_id` — so channel → owner lookup works
against them unchanged on a Supabase install. What they *also* carry is
`encrypted_token`: an AES-encrypted copy of the user's API token, which the
backend no longer reads and no longer wants at rest. This blanks that column.

Once every deployment has run this, drop the column entirely:

    ALTER TABLE messaging_configs DROP COLUMN encrypted_token;

DynamoDB installs need nothing here — the table was never populated by the old
bot. `yolo install` adds the `platform_channel-index` GSI that
channel lookup requires.

Usage:
    uv run --env-file ../.env python migrate_messaging_configs.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }


def fetch_rows() -> list[dict]:
    qs = urllib.parse.urlencode({"select": "id,platform,api_token_id,encrypted_token"})
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/messaging_configs?{qs}",
        method="GET",
        headers=_headers(),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def blank_token(config_id: str) -> None:
    qs = urllib.parse.urlencode({"id": f"eq.{config_id}"})
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/messaging_configs?{qs}",
        method="PATCH",
        headers={**_headers(), "Prefer": "return=minimal"},
        # Empty string rather than NULL: the column may be NOT NULL, and this
        # runs before the column is dropped.
        data=json.dumps({"encrypted_token": ""}).encode(),
    )
    urllib.request.urlopen(req)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.", file=sys.stderr)
        print("DynamoDB installs need no migration — see the module docstring.", file=sys.stderr)
        return 1

    try:
        rows = fetch_rows()
    except urllib.error.HTTPError as exc:
        print(f"Failed to read messaging_configs: {exc}", file=sys.stderr)
        return 1

    pending = [r for r in rows if r.get("encrypted_token")]
    print(f"{len(rows)} config(s) total, {len(pending)} still holding an encrypted token.")

    # A row missing api_token_id cannot be resolved to an owner, so its channel
    # would silently stop responding. Surface it rather than blanking it.
    orphans = [r for r in rows if not r.get("api_token_id")]
    if orphans:
        print(f"\nWARNING: {len(orphans)} config(s) have no api_token_id and will not resolve:")
        for r in orphans:
            print(f"  - {r['id']} ({r.get('platform', '?')}) — re-run /setup on that channel")

    if not pending:
        print("\nNothing to do.")
        return 0

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    failed = 0
    for row in pending:
        try:
            blank_token(row["id"])
        except urllib.error.HTTPError as exc:
            print(f"  FAILED {row['id']}: {exc}", file=sys.stderr)
            failed += 1

    print(f"\nCleared {len(pending) - failed} token(s); {failed} failure(s).")
    if failed == 0:
        print("Safe to run: ALTER TABLE messaging_configs DROP COLUMN encrypted_token;")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
