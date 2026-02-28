#!/usr/bin/env bash
# Build the Vite SPA and deploy it to s3://$FRONTEND_BUCKET/$VITE_SITE/.
#
# Bucket layout:
#   {bucket}/.skills/          ← site-scoped skills (never touched by this script)
#   {bucket}/{site}/index.html ← SPA entry point
#   {bucket}/{site}/assets/    ← hashed JS/CSS bundles
#   {bucket}/{site}/content.md ← wiki content (never touched by this script)
#
# Required in environment or root .env:
#   VITE_API_BASE               — HTTPS URL of the backend ALB
#                                 (e.g. https://agentscribe-dev.runyolo.dev)
#   VITE_SITE                   — site name; also used as the S3 deploy prefix
#   VITE_SUPABASE_URL           — Supabase project URL
#   VITE_SUPABASE_ANON_KEY      — Supabase anon key
#   FRONTEND_BUCKET             — root S3 bucket (e.g. agentscribe-dev)
#
# Optional:
#   CLOUDFRONT_DISTRIBUTION_ID  — if set, invalidates /* after sync
#   AWS_PROFILE                 — named AWS profile for s3/cloudfront commands
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../frontend"
ENV_FILE="$SCRIPT_DIR/../.env"

# Load root .env if present
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${VITE_API_BASE:-}" ]]; then
  echo "Error: VITE_API_BASE is not set (e.g. https://agentscribe-dev.runyolo.dev)"
  exit 1
fi

if [[ -z "${FRONTEND_BUCKET:-}" ]]; then
  echo "Error: FRONTEND_BUCKET is not set (S3 bucket that serves the static site)"
  exit 1
fi

if [[ -z "${VITE_SITE:-}" ]]; then
  echo "Error: VITE_SITE is not set in frontend/.env.local or environment"
  exit 1
fi

if [[ -z "${VITE_SUPABASE_URL:-}" ]]; then
  echo "Error: VITE_SUPABASE_URL is not set (e.g. https://<project-ref>.supabase.co)"
  exit 1
fi

if [[ -z "${VITE_SUPABASE_ANON_KEY:-}" ]]; then
  echo "Error: VITE_SUPABASE_ANON_KEY is not set"
  exit 1
fi

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

echo "── Building frontend ────────────────────────────────────────────────────────"
echo "  VITE_API_BASE        = $VITE_API_BASE"
echo "  VITE_SITE            = $VITE_SITE"
echo "  VITE_SUPABASE_URL    = $VITE_SUPABASE_URL"
echo "  VITE_SUPABASE_ANON_KEY = (set)"
echo ""

cd "$FRONTEND_DIR"
npm ci --silent
VITE_API_BASE="$VITE_API_BASE" \
  VITE_SITE="$VITE_SITE" \
  VITE_SUPABASE_URL="$VITE_SUPABASE_URL" \
  VITE_SUPABASE_ANON_KEY="$VITE_SUPABASE_ANON_KEY" \
  npm run build

SITE_PREFIX="$FRONTEND_BUCKET/$VITE_SITE"

echo ""
echo "── Uploading dist/ → s3://$SITE_PREFIX/ ────────────────────────────────────"

# Upload hashed assets with aggressive caching. No --delete: user wiki data
# lives in the same bucket; old asset hashes are harmless stale files.
aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} s3 sync dist/assets/ "s3://$SITE_PREFIX/assets/" \
  --cache-control "public,max-age=31536000,immutable"

# index.html must not be cached — browsers re-check it on every load
aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} s3 cp dist/index.html "s3://$SITE_PREFIX/index.html" \
  --cache-control "no-cache,no-store,must-revalidate" \
  --content-type "text/html"

if [[ -n "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  echo ""
  echo "── Invalidating CloudFront distribution $CLOUDFRONT_DISTRIBUTION_ID ──────"
  aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
    --paths "/*" \
    --query 'Invalidation.Id' --output text
fi

echo ""
echo "Done."
