"""Migrate user→site data from Supabase into DynamoDB (Item 3, one-off).

When cutting an install over from AUTH_PROVIDER=supabase to a DynamoDB-backed
provider (cognito or the generic oidc), the user→site mapping must move from the
Supabase `user_site` table into the DynamoDB user-site table. This copies every
row, reusing the real DynamoDBUserSiteRepository write path so the item shape
matches exactly what the backend reads.

Idempotent — rows that already exist in DynamoDB are skipped (the repository's
insert is conditional on the key not existing), so it is safe to re-run.

Scope: this migrates the user→site mapping ONLY. That is a deliberate decision,
not an omission, and the three tables move (or don't) as a set:

  user_site         — migrated here. Without it a user cannot resolve their site,
                      so there is no way to recreate it from the user's side.
  api_tokens        — NOT migrated. Users generate a new token after cutover.
  messaging_configs — NOT migrated, and cannot usefully be. A binding's only link
                      to an owner is `api_token_id`; regenerated tokens get new
                      UUIDs, so migrated rows would reference IDs that do not
                      exist and every channel would resolve to "not linked".
                      Users re-run /setup per channel, which they must do anyway
                      once their token changes.

If you ever want a silent cutover instead, migrate `api_tokens` preserving `id`
and `token_hash` (a direct put_item — insert_token mints a fresh UUID, so the
repository write path cannot be reused for this), and then migrating
messaging_configs becomes meaningful. Both or neither; migrating
messaging_configs alone produces rows that look correct and are all dead.

Source (Supabase) env:   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Dest (DynamoDB) env:     AWS creds/region, DYNAMODB_USER_SITE_TABLE (optional)

In-cluster (the backend pod already has AWS + Supabase env):

    kubectl exec deploy/yoloscribe-backend -n yolo -- \
      python migrate_supabase_to_dynamodb.py

Locally (real dev account):

    AWS_PROFILE=runyolo_admin AWS_REGION=us-west-2 \
      SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
      uv run python migrate_supabase_to_dynamodb.py [--dry-run]
"""

from __future__ import annotations

import os
import sys

import httpx
from fastapi import HTTPException

from auth_providers.dynamodb import DynamoDBUserSiteRepository


def _supabase_user_site_rows() -> list[dict]:
    """Every row from the Supabase user_site table (user_uuid, site_name, theme)."""
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = httpx.get(
        f"{url}/rest/v1/user_site?select=user_uuid,site_name,theme",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    dry_run = "--dry-run" in args

    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if not os.environ.get(v)]
    if missing:
        print(f"Missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        raise SystemExit(1)

    region = os.environ.get("AWS_REGION", "us-west-2")
    table = os.environ.get("DYNAMODB_USER_SITE_TABLE", "yoloscribe-user-site")
    repo = DynamoDBUserSiteRepository(table, region)

    rows = _supabase_user_site_rows()
    print(f"Found {len(rows)} row(s) in Supabase user_site.")
    print(f"Destination: DynamoDB table '{table}' in {region}"
          f"{'  (DRY RUN — no writes)' if dry_run else ''}\n")

    migrated = skipped = failed = 0
    for row in rows:
        user_id = row.get("user_uuid")
        site_name = row.get("site_name")
        theme = row.get("theme") or "dark"
        if not user_id or not site_name:
            failed += 1
            print(f"  SKIP    {user_id!r}  (missing user_uuid or site_name)")
            continue

        if dry_run:
            print(f"  would migrate  {user_id}  ->  {site_name}  (theme={theme})")
            migrated += 1
            continue

        try:
            repo.insert_user_site(user_id, site_name, theme)
            migrated += 1
            print(f"  migrated  {user_id}  ->  {site_name}")
        except HTTPException as exc:
            if exc.status_code == 409:
                skipped += 1
                print(f"  skip      {user_id}  (already in DynamoDB)")
            else:
                failed += 1
                print(f"  FAILED    {user_id}  ({exc.detail})")

    verb = "would migrate" if dry_run else "migrated"
    print(f"\nDone: {migrated} {verb}, {skipped} skipped, {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
