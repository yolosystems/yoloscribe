#!/usr/bin/env bash
# setup_dynamodb.sh — Create DynamoDB tables for YoloScribe Cognito operators.
#
# Run once before the first Helm install. Idempotent — safe to re-run.
#
# Usage:
#   AWS_PROFILE=myprofile AWS_REGION=us-east-1 ./scripts/setup_dynamodb.sh
#
# Override table names with env vars:
#   DYNAMODB_USER_SITE_TABLE=yoloscribe-user-site
#   DYNAMODB_API_TOKENS_TABLE=yoloscribe-api-tokens

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
USER_SITE_TABLE="${DYNAMODB_USER_SITE_TABLE:-yoloscribe-user-site}"
API_TOKENS_TABLE="${DYNAMODB_API_TOKENS_TABLE:-yoloscribe-api-tokens}"
AGENT_LOCKS_TABLE="${DYNAMODB_AGENT_LOCKS_TABLE:-yoloscribe-agent-locks}"
ENDPOINT="${DYNAMODB_ENDPOINT_URL:-}"  # set for local testing (e.g. http://localhost:8000)

aws_cmd() {
  if [[ -n "${ENDPOINT}" ]]; then
    aws --region "${REGION}" --endpoint-url "${ENDPOINT}" "$@"
  else
    aws --region "${REGION}" "$@"
  fi
}

table_exists() {
  aws_cmd dynamodb describe-table --table-name "$1" > /dev/null 2>&1
}

echo "Region:            ${REGION}"
echo "User-site table:   ${USER_SITE_TABLE}"
echo "API-tokens table:  ${API_TOKENS_TABLE}"
echo "Agent-locks table: ${AGENT_LOCKS_TABLE}"
echo ""

# ---------------------------------------------------------------------------
# yoloscribe-user-site
# PK: user_id (S)
# ---------------------------------------------------------------------------

if table_exists "${USER_SITE_TABLE}"; then
  echo "✓ Table '${USER_SITE_TABLE}' already exists — skipping."
else
  echo "Creating '${USER_SITE_TABLE}'..."
  aws_cmd dynamodb create-table \
    --table-name "${USER_SITE_TABLE}" \
    --attribute-definitions \
      AttributeName=user_id,AttributeType=S \
    --key-schema \
      AttributeName=user_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --tags Key=app,Value=yoloscribe
  echo "✓ Created '${USER_SITE_TABLE}'."
fi

# ---------------------------------------------------------------------------
# yoloscribe-api-tokens
# PK:   token_id (S)
# GSI1: user_id-index  — PK: user_id,  SK: created_at  (for listing tokens)
# GSI2: token_hash-index — PK: token_hash              (for auth lookup)
# ---------------------------------------------------------------------------

if table_exists "${API_TOKENS_TABLE}"; then
  echo "✓ Table '${API_TOKENS_TABLE}' already exists — skipping."
else
  echo "Creating '${API_TOKENS_TABLE}'..."
  aws_cmd dynamodb create-table \
    --table-name "${API_TOKENS_TABLE}" \
    --attribute-definitions \
      AttributeName=token_id,AttributeType=S \
      AttributeName=user_id,AttributeType=S \
      AttributeName=created_at,AttributeType=S \
      AttributeName=token_hash,AttributeType=S \
    --key-schema \
      AttributeName=token_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --global-secondary-indexes \
      '[
        {
          "IndexName": "user_id-index",
          "KeySchema": [
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"}
          ],
          "Projection": {"ProjectionType": "ALL"}
        },
        {
          "IndexName": "token_hash-index",
          "KeySchema": [
            {"AttributeName": "token_hash", "KeyType": "HASH"}
          ],
          "Projection": {"ProjectionType": "ALL"}
        }
      ]' \
    --tags Key=app,Value=yoloscribe
  echo "✓ Created '${API_TOKENS_TABLE}'."
fi


# ---------------------------------------------------------------------------
# yoloscribe-agent-locks
# PK: user_id   (S)
# SK: page_path (S)
# TTL on expires_at — prevents stale locks if an agent pod crashes
# ---------------------------------------------------------------------------

if table_exists "${AGENT_LOCKS_TABLE}"; then
  echo "✓ Table '${AGENT_LOCKS_TABLE}' already exists — skipping."
else
  echo "Creating '${AGENT_LOCKS_TABLE}'..."
  aws_cmd dynamodb create-table \
    --table-name "${AGENT_LOCKS_TABLE}" \
    --attribute-definitions \
      AttributeName=user_id,AttributeType=S \
      AttributeName=page_path,AttributeType=S \
    --key-schema \
      AttributeName=user_id,KeyType=HASH \
      AttributeName=page_path,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --tags Key=app,Value=yoloscribe
  aws_cmd dynamodb update-time-to-live \
    --table-name "${AGENT_LOCKS_TABLE}" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at"
  echo "✓ Created '${AGENT_LOCKS_TABLE}'."
fi

echo ""
echo "Done. All tables are ready."
