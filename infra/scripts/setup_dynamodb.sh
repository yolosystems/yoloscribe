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
#   DYNAMODB_MESSAGING_CONFIGS_TABLE=yoloscribe-messaging-configs

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
USER_SITE_TABLE="${DYNAMODB_USER_SITE_TABLE:-yoloscribe-user-site}"
API_TOKENS_TABLE="${DYNAMODB_API_TOKENS_TABLE:-yoloscribe-api-tokens}"
MESSAGING_CONFIGS_TABLE="${DYNAMODB_MESSAGING_CONFIGS_TABLE:-yoloscribe-messaging-configs}"
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

echo "Region:                 ${REGION}"
echo "User-site table:        ${USER_SITE_TABLE}"
echo "API-tokens table:       ${API_TOKENS_TABLE}"
echo "Messaging-configs table: ${MESSAGING_CONFIGS_TABLE}"
echo "Agent-locks table:      ${AGENT_LOCKS_TABLE}"
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
# yoloscribe-messaging-configs
# PK:   id (S)
# GSI1: api_token_id-index     — PK: api_token_id     (list configs for a site's tokens)
# GSI2: platform_channel-index — PK: platform_channel (resolve an inbound message)
#
# platform_channel is a derived attribute, "{platform}:{channel_id}", because
# DynamoDB cannot index into the nested `connection` map. It is written on every
# upsert and must stay consistent with connection.channel_id.
#
# Rows are written by the backend's internal messaging endpoints (YOL-523); the
# messaging bot has no database access of its own.
# ---------------------------------------------------------------------------

if table_exists "${MESSAGING_CONFIGS_TABLE}"; then
  echo "✓ Table '${MESSAGING_CONFIGS_TABLE}' already exists — skipping creation."
  # Older deployments predate platform_channel-index; add it in place. A table
  # can only take one GSI addition at a time, so this is its own call.
  if aws_cmd dynamodb describe-table --table-name "${MESSAGING_CONFIGS_TABLE}" \
      --query 'Table.GlobalSecondaryIndexes[?IndexName==`platform_channel-index`]' \
      --output text 2>/dev/null | grep -q .; then
    echo "✓ GSI 'platform_channel-index' already present."
  else
    echo "Adding missing GSI 'platform_channel-index'..."
    # update-table also requires an ACTIVE table; a previous partial run can
    # leave it UPDATING.
    aws_cmd dynamodb wait table-exists --table-name "${MESSAGING_CONFIGS_TABLE}"
    aws_cmd dynamodb update-table \
      --table-name "${MESSAGING_CONFIGS_TABLE}" \
      --attribute-definitions AttributeName=platform_channel,AttributeType=S \
      --global-secondary-index-updates \
        '[
          {
            "Create": {
              "IndexName": "platform_channel-index",
              "KeySchema": [
                {"AttributeName": "platform_channel", "KeyType": "HASH"}
              ],
              "Projection": {"ProjectionType": "ALL"}
            }
          }
        ]'
    echo "✓ GSI creation started (backfills asynchronously)."
  fi
else
  echo "Creating '${MESSAGING_CONFIGS_TABLE}'..."
  aws_cmd dynamodb create-table \
    --table-name "${MESSAGING_CONFIGS_TABLE}" \
    --attribute-definitions \
      AttributeName=id,AttributeType=S \
      AttributeName=api_token_id,AttributeType=S \
      AttributeName=platform_channel,AttributeType=S \
    --key-schema \
      AttributeName=id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --global-secondary-indexes \
      '[
        {
          "IndexName": "api_token_id-index",
          "KeySchema": [
            {"AttributeName": "api_token_id", "KeyType": "HASH"}
          ],
          "Projection": {"ProjectionType": "ALL"}
        },
        {
          "IndexName": "platform_channel-index",
          "KeySchema": [
            {"AttributeName": "platform_channel", "KeyType": "HASH"}
          ],
          "Projection": {"ProjectionType": "ALL"}
        }
      ]' \
    --tags Key=app,Value=yoloscribe
  echo "✓ Created '${MESSAGING_CONFIGS_TABLE}'."
fi

# ---------------------------------------------------------------------------
# yoloscribe-agent-locks
# PK: user_id   (S)
# SK: page_path (S)
# TTL on expires_at — prevents stale locks if an agent pod crashes
# ---------------------------------------------------------------------------

if table_exists "${AGENT_LOCKS_TABLE}"; then
  echo "✓ Table '${AGENT_LOCKS_TABLE}' already exists — skipping creation."
  # TTL is a separate call from create-table, so a run that failed between the
  # two leaves the table present but without TTL. Repair that rather than
  # skipping past it — stale locks would otherwise never expire.
  TTL_STATUS="$(aws_cmd dynamodb describe-time-to-live \
    --table-name "${AGENT_LOCKS_TABLE}" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || echo UNKNOWN)"
  if [[ "${TTL_STATUS}" == "ENABLED" || "${TTL_STATUS}" == "ENABLING" ]]; then
    echo "✓ TTL on 'expires_at' already ${TTL_STATUS}."
  else
    echo "Enabling missing TTL on 'expires_at' (status was ${TTL_STATUS})..."
    aws_cmd dynamodb wait table-exists --table-name "${AGENT_LOCKS_TABLE}"
    aws_cmd dynamodb update-time-to-live \
      --table-name "${AGENT_LOCKS_TABLE}" \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at"
    echo "✓ TTL enabled."
  fi
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
  # create-table returns while the table is still CREATING, and TTL can only be
  # set on an ACTIVE table — without this wait the next call fails with
  # ResourceNotFoundException.
  echo "  waiting for '${AGENT_LOCKS_TABLE}' to become ACTIVE..."
  aws_cmd dynamodb wait table-exists --table-name "${AGENT_LOCKS_TABLE}"
  aws_cmd dynamodb update-time-to-live \
    --table-name "${AGENT_LOCKS_TABLE}" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at"
  echo "✓ Created '${AGENT_LOCKS_TABLE}'."
fi

echo ""
echo "Done. All tables are ready."
