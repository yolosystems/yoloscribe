#!/usr/bin/env bash
# One-time setup: create the IAM role for the agentscribe-indexer
# (shared by both the polling-worker Deployment and the indexer Jobs).
#
# The role is annotated with an IRSA trust policy scoped to the
# agentscribe-indexer Kubernetes ServiceAccount.
#
# Prerequisites:
#   - AWS CLI configured with credentials that can create IAM roles
#   - The EKS cluster's OIDC provider URL (without https://)
#
# Usage:
#   EKS_OIDC_PROVIDER=oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633 \
#   AWS_ACCOUNT_ID=123456789012 \
#   AWS_REGION=us-east-1 \
#   K8S_NAMESPACE=agentscribe \
#   SQS_INDEXING_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123456789012/agentscribe-indexing \
#   S3_BUCKET=my-agentscribe-bucket \
#   S3_VECTORS_BUCKET=my-agentscribe-vectors \
#   S3_VECTORS_INDEX_NAME=agentscribe \
#   BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0 \
#   bash create_indexer_role.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Load root .env if present
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

: "${EKS_OIDC_PROVIDER:?EKS_OIDC_PROVIDER must be set (without https://)}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID must be set}"
: "${AWS_REGION:?AWS_REGION must be set}"
: "${K8S_NAMESPACE:?K8S_NAMESPACE must be set}"
: "${SQS_INDEXING_QUEUE_URL:?SQS_INDEXING_QUEUE_URL must be set}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${S3_VECTORS_BUCKET:?S3_VECTORS_BUCKET must be set}"
: "${S3_VECTORS_INDEX_NAME:=${S3_VECTORS_INDEX_NAME:-agentscribe}}"
: "${BEDROCK_EMBEDDING_MODEL:=${BEDROCK_EMBEDDING_MODEL:-amazon.titan-embed-text-v2:0}}"

ROLE_NAME="agentscribe-indexer"
SA_NAME="agentscribe-indexer"

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

# Derive SQS queue ARN from URL:
# https://sqs.{region}.amazonaws.com/{account}/{name}  →  arn:aws:sqs:{region}:{account}:{name}
SQS_QUEUE_ARN=$(aws "${AWS_ARGS[@]}" sqs get-queue-attributes \
  --queue-url "$SQS_INDEXING_QUEUE_URL" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

echo "SQS queue ARN: $SQS_QUEUE_ARN"

# Build policy document by substituting placeholders in the template
POLICY_DOCUMENT=$(sed \
  -e "s|__SQS_INDEXING_QUEUE_ARN__|${SQS_QUEUE_ARN}|g" \
  -e "s|__S3_BUCKET__|${S3_BUCKET}|g" \
  -e "s|__AWS_REGION__|${AWS_REGION}|g" \
  -e "s|__BEDROCK_EMBEDDING_MODEL__|${BEDROCK_EMBEDDING_MODEL}|g" \
  -e "s|__S3_VECTORS_BUCKET__|${S3_VECTORS_BUCKET}|g" \
  -e "s|__S3_VECTORS_INDEX_NAME__|${S3_VECTORS_INDEX_NAME}|g" \
  -e "s|__AWS_ACCOUNT_ID__|${AWS_ACCOUNT_ID}|g" \
  "$SCRIPT_DIR/agentscribe-indexer-policy.json")

TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${EKS_OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${EKS_OIDC_PROVIDER}:sub": "system:serviceaccount:${K8S_NAMESPACE}:${SA_NAME}",
          "${EKS_OIDC_PROVIDER}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF
)

echo "Creating IAM role: $ROLE_NAME"

aws "${AWS_ARGS[@]}" iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --description "IRSA role for agentscribe indexer (polling worker + indexer jobs)" \
  --output json | jq -r '.Role.Arn'

echo "Attaching inline policy..."
aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "agentscribe-indexer-access" \
  --policy-document "$POLICY_DOCUMENT"

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Done. Role ARN:"
echo "  $ROLE_ARN"
echo ""
echo "Set this in your environment before running install_indexer.sh:"
echo "  export INDEXER_ROLE_ARN=$ROLE_ARN"
