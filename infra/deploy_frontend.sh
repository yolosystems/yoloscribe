#!/usr/bin/env bash
# Build the Vite SPA and sync it to the frontend S3 bucket.
#
# Reads build-time vars from frontend/.env.local:
#   VITE_SITE        — default site name (fallback when URL has no path segment)
#   VITE_S3_BUCKET   — content S3 bucket name
#
# Required in environment or backend/.env:
#   VITE_API_BASE               — HTTPS URL of the backend ALB
#                                 (e.g. https://agentscribe-dev.runyolo.dev)
#   FRONTEND_BUCKET             — S3 bucket that serves the static site
#                                 (e.g. agentscribe-dev-frontend)
#
# Optional:
#   CLOUDFRONT_DISTRIBUTION_ID  — if set, invalidates /* after sync
#   AWS_PROFILE                 — named AWS profile for s3/cloudfront commands
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../frontend"
BACKEND_ENV="$SCRIPT_DIR/../backend/.env"
FRONTEND_ENV="$FRONTEND_DIR/.env.local"

# Load backend/.env (AWS creds, VITE_API_BASE, FRONTEND_BUCKET, etc.)
if [[ -f "$BACKEND_ENV" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$BACKEND_ENV"
  set +a
fi

# Load frontend/.env.local (VITE_SITE, VITE_S3_BUCKET)
if [[ -f "$FRONTEND_ENV" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$FRONTEND_ENV"
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

if [[ -z "${VITE_S3_BUCKET:-}" ]]; then
  echo "Error: VITE_S3_BUCKET is not set in frontend/.env.local or environment"
  exit 1
fi

if [[ -z "${VITE_SITE:-}" ]]; then
  echo "Error: VITE_SITE is not set in frontend/.env.local or environment"
  exit 1
fi

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

echo "── Building frontend ────────────────────────────────────────────────────────"
echo "  VITE_API_BASE  = $VITE_API_BASE"
echo "  VITE_S3_BUCKET = $VITE_S3_BUCKET"
echo "  VITE_SITE      = $VITE_SITE"
echo ""

cd "$FRONTEND_DIR"
npm ci --silent
VITE_API_BASE="$VITE_API_BASE" \
  VITE_S3_BUCKET="$VITE_S3_BUCKET" \
  VITE_SITE="$VITE_SITE" \
  npm run build

echo ""
echo "── Syncing dist/ → s3://$FRONTEND_BUCKET/ ──────────────────────────────────"

# Hashed assets can be cached aggressively; everything else must not be cached
aws "${AWS_ARGS[@]}" s3 sync dist/ "s3://$FRONTEND_BUCKET/" \
  --delete \
  --cache-control "public,max-age=31536000,immutable" \
  --exclude "index.html"

# index.html must not be cached — browsers re-check it on every load
aws "${AWS_ARGS[@]}" s3 cp dist/index.html "s3://$FRONTEND_BUCKET/index.html" \
  --cache-control "no-cache,no-store,must-revalidate" \
  --content-type "text/html"

if [[ -n "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  echo ""
  echo "── Invalidating CloudFront distribution $CLOUDFRONT_DISTRIBUTION_ID ──────"
  aws "${AWS_ARGS[@]}" cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
    --paths "/*" \
    --query 'Invalidation.Id' --output text
fi

echo ""
echo "Done."
