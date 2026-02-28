#!/usr/bin/env bash
# Build all 3 theme SPA variants and upload to S3 under _themes/{theme}/
# Usage: S3_BUCKET=my-bucket ./scripts/build-themes.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="$ROOT_DIR/frontend"

if [[ -z "${S3_BUCKET:-}" ]]; then
  # Try loading from .env at project root
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

for THEME in light dark yolo; do
  echo "Building theme: $THEME"
  VITE_THEME="$THEME" npm run build --prefix "$FRONTEND_DIR"
  echo "Uploading $THEME to s3://$S3_BUCKET/_themes/$THEME/"
  aws s3 sync "$FRONTEND_DIR/dist/" "s3://$S3_BUCKET/_themes/$THEME/" --delete --profile ${AWS_PROFILE}
  echo "Done: $THEME"
done

echo "All themes built and uploaded."
