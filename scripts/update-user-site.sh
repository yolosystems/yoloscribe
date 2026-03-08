#!/usr/bin/env bash
# Push a pre-built theme bundle to an existing user site in S3.
# Copies from _themes/{theme}/ → {site_name}/ without touching user content.
#
# Usage:
#   ./scripts/update-user-site.sh --site-name knuth --theme yolo
#   ./scripts/update-user-site.sh --root --theme dark   # updates the bucket root (main site)
#
# Required env (or set in .env at project root):
#   S3_BUCKET                  — S3 bucket name
#
# Optional env:
#   AWS_PROFILE                — named AWS profile (omit to use the default credential chain)
#   CLOUDFRONT_DISTRIBUTION_ID — if set, invalidates index.html and config.json
#                                so the CDN serves the new files immediately

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Parse arguments ────────────────────────────────────────────────────────────

SITE_NAME=""
THEME=""
ROOT_SITE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-name) SITE_NAME="$2"; shift 2 ;;
    --theme)     THEME="$2";     shift 2 ;;
    --root)      ROOT_SITE=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$THEME" ]]; then
  echo "Usage: $0 --site-name <name> --theme <light|dark|yolo>" >&2
  echo "       $0 --root --theme <light|dark|yolo>" >&2
  exit 1
fi

if [[ "$ROOT_SITE" == false && -z "$SITE_NAME" ]]; then
  echo "Error: provide either --site-name <name> or --root" >&2
  exit 1
fi

VALID_THEMES="light dark yolo"
if ! echo "$VALID_THEMES" | grep -qw "$THEME"; then
  echo "Error: theme must be one of: $VALID_THEMES" >&2
  exit 1
fi

# ── Load env vars ─────────────────────────────────────────────────────────────

if [[ -z "${S3_BUCKET:-}" ]]; then
  ENV_FILE="$ROOT_DIR/.env"
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
  fi
fi

if [[ -z "${S3_BUCKET:-}" ]]; then
  echo "Error: S3_BUCKET is not set" >&2
  exit 1
fi

# CLOUDFRONT_DISTRIBUTION_ID is optional; if set, the script will invalidate
# the affected paths so the CDN serves the new files immediately.
CF_DIST_ID="${CLOUDFRONT_DISTRIBUTION_ID:-}"

# ── Build AWS CLI invocation ───────────────────────────────────────────────────

if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS="aws --profile $AWS_PROFILE"
else
  AWS="aws"
fi

SRC="s3://$S3_BUCKET/_themes/$THEME/"
if [[ "$ROOT_SITE" == true ]]; then
  DST="s3://$S3_BUCKET/"
  CF_INVALIDATION_PATHS=("/index.html")  # root site has no config.json theme
else
  DST="s3://$S3_BUCKET/$SITE_NAME/"
  CF_INVALIDATION_PATHS=("/$SITE_NAME/index.html" "/$SITE_NAME/config.json")
fi

# ── Verify source theme exists ─────────────────────────────────────────────────

echo "Checking source theme: $SRC"
THEME_KEY_COUNT=$($AWS s3 ls "$SRC" | wc -l | tr -d ' ')
if [[ "$THEME_KEY_COUNT" -eq 0 ]]; then
  echo "Error: theme '$THEME' not found at $SRC" >&2
  echo "Run scripts/build-themes.sh first to build and upload the theme bundles." >&2
  exit 1
fi

# ── Copy static assets (long cache TTL, skip index.html) ──────────────────────

echo "Syncing assets: $SRC → $DST"
$AWS s3 sync "$SRC" "$DST" \
  --exclude "index.html" \
  --cache-control "max-age=31536000,immutable" \
  --no-progress

# ── Copy index.html (no-cache so updates are picked up immediately) ────────────

echo "Updating index.html (no-cache)"
$AWS s3 cp "${SRC}index.html" "${DST}index.html" \
  --cache-control "no-cache" \
  --content-type "text/html"

# ── Update config.json to record the active theme (user sites only) ────────────

if [[ "$ROOT_SITE" == false ]]; then
  echo "Updating config.json (theme: $THEME)"
  echo "{\"theme\": \"$THEME\"}" | $AWS s3 cp - "${DST}config.json" \
    --content-type "application/json" \
    --cache-control "no-cache"
fi

# ── Invalidate CloudFront cache ────────────────────────────────────────────────

if [[ -n "$CF_DIST_ID" ]]; then
  echo "Invalidating CloudFront cache for: ${CF_INVALIDATION_PATHS[*]}"
  $AWS cloudfront create-invalidation \
    --distribution-id "$CF_DIST_ID" \
    --paths "${CF_INVALIDATION_PATHS[@]}" \
    --query 'Invalidation.Id' \
    --output text
else
  echo "Warning: CLOUDFRONT_DISTRIBUTION_ID is not set — skipping cache invalidation." >&2
  echo "         Set it in .env or export it to avoid stale cached responses." >&2
fi

echo ""
if [[ "$ROOT_SITE" == true ]]; then
  echo "Done. Root (main) site is now running the '$THEME' theme."
else
  echo "Done. Site '$SITE_NAME' is now running the '$THEME' theme."
fi
