#!/usr/bin/env bash
set -euo pipefail

BUCKET="yoloscribe-dev"  # Phase 3: rename to yoloscribe-dev
SITE="knuth"

# Set VITE_API_BASE to your ALB URL before running, e.g.:
#   VITE_API_BASE=https://your-alb.amazonaws.com ./scripts/deploy.sh
API_BASE="${VITE_API_BASE:-}"

if [[ -z "$API_BASE" ]]; then
  echo "Error: VITE_API_BASE is not set. Export it before running:"
  echo "  export VITE_API_BASE=https://your-alb.amazonaws.com"
  exit 1
fi

if [[ -z "${AWS_PROFILE:-}" ]]; then
  echo "Error: AWS_PROFILE is not set. Export it before running:"
  echo "  export AWS_PROFILE=my-profile"
  exit 1
fi

AWS="aws --profile $AWS_PROFILE"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/../../frontend"

echo "==> Building frontend (API base: $API_BASE)"
cd "$FRONTEND_DIR"
VITE_API_BASE="$API_BASE" npm run build

echo "==> Uploading assets to s3://$BUCKET/$SITE/"
$AWS s3 sync dist/ "s3://$BUCKET/$SITE/" \
  --cache-control "max-age=31536000,immutable" \
  --exclude "index.html" \
  --exclude "content.md"

echo "==> Uploading index.html (no-cache)"
$AWS s3 cp dist/index.html "s3://$BUCKET/$SITE/index.html" \
  --cache-control "no-cache" \
  --content-type "text/html"

echo "==> Checking for existing content.md"
if $AWS s3 ls "s3://$BUCKET/$SITE/content.md" &>/dev/null; then
  echo "    content.md already exists — skipping to preserve live content"
else
  echo "    Uploading initial content.md"
  $AWS s3 cp dist/content.md "s3://$BUCKET/$SITE/content.md" \
    --content-type "text/markdown"
fi

echo ""
echo "Done! Site available at:"
echo "  http://$BUCKET.s3-website-$($AWS configure get region).amazonaws.com/$SITE/"
