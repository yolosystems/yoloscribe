#!/usr/bin/env bash
# Calls POST /webhooks/user-created to provision IAM role, K8s ServiceAccount,
# and Secrets Manager placeholder for a user.
#
# Usage:
#   ./provision_user.sh              # provisions default dev user "knuth"
#   ./provision_user.sh alice        # provisions user "alice"
#
# Requires WEBHOOK_SECRET to be set in backend/.env (or the environment).
# If WEBHOOK_SECRET is empty the backend will reject the request.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

USER_ID="${1:-knuth}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
SECRET="${WEBHOOK_SECRET:-}"

echo "Provisioning user: $USER_ID"
echo "Endpoint: $BASE_URL/webhooks/user-created"

curl -s -w "\nHTTP %{http_code}\n" \
  -X POST "$BASE_URL/webhooks/user-created" \
  -H "Content-Type: application/json" \
  -H "x-webhook-secret: $SECRET" \
  -d "{\"user_id\": \"$USER_ID\"}" | cat
