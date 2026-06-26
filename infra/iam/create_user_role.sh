#!/usr/bin/env bash
# Create or update the IRSA role and K8s ServiceAccount for a YoloScribe user.
#
# This mirrors what the backend's provision_user_infrastructure() does, for
# use when you need to (re)provision a user without the backend webhook running
# — e.g. manual dev setup or policy updates for existing users.
#
# Prerequisites:
#   - AWS CLI configured with admin credentials
#   - kubectl configured against the target EKS cluster
#
# Usage:
#   USER_ID=knuth SITE_NAME=knuth \
#   EKS_OIDC_PROVIDER=oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633 \
#   AWS_ACCOUNT_ID=123456789012 \
#   AWS_REGION=us-west-2 \
#   K8S_NAMESPACE=yolo \
#   S3_BUCKET=yoloscribe-prod \
#   SQS_QUEUE_ARN=arn:aws:sqs:us-west-2:123456789012:yoloscribe-agents \
#   SQS_INDEXING_QUEUE_ARN=arn:aws:sqs:us-west-2:123456789012:yoloscribe-indexing \
#   AWS_PROFILE=myprofile \
#   bash create_user_role.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

: "${USER_ID:?USER_ID must be set (e.g. knuth)}"
: "${SITE_NAME:?SITE_NAME must be set (e.g. knuth-home)}"
: "${EKS_OIDC_PROVIDER:?EKS_OIDC_PROVIDER must be set (without https://)}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID must be set}"
: "${AWS_REGION:=us-west-2}"
: "${K8S_NAMESPACE:=yolo}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${SQS_QUEUE_ARN:?SQS_QUEUE_ARN must be set}"
: "${SQS_INDEXING_QUEUE_ARN:?SQS_INDEXING_QUEUE_ARN must be set}"
: "${DDB_AGENT_LOCKS_TABLE:=yoloscribe-agent-locks}"

ROLE_NAME="yoloscribe-user-${USER_ID}"
SA_NAME="user-${USER_ID}"

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

# ── Trust policy ───────────────────────────────────────────────────────────────

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

if aws "${AWS_ARGS[@]}" iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
  echo "IAM role '${ROLE_NAME}' already exists — updating inline policy only."
else
  echo "Creating IAM role: ${ROLE_NAME}"
  aws "${AWS_ARGS[@]}" iam create-role \
    --role-name "$ROLE_NAME" \
    --path "/yoloscribe/" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "IRSA role for YoloScribe user ${USER_ID}" \
    --output json | jq -r '.Role.Arn'
fi

# ── Inline policy (specific ARNs — no wildcards) ───────────────────────────────

S3_BUCKET_ARN="arn:aws:s3:::${S3_BUCKET}"
SM_SECRET_PREFIX="arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:yoloscribe/${USER_ID}/"
DDB_TABLE_ARN="arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${DDB_AGENT_LOCKS_TABLE}"

INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsManagerUserSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue"
      ],
      "Resource": "${SM_SECRET_PREFIX}*"
    },
    {
      "Sid": "S3ReadWriteUserPrefix",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "${S3_BUCKET_ARN}/${SITE_NAME}/*"
    },
    {
      "Sid": "S3ReadToolsPrefix",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "${S3_BUCKET_ARN}/.tools/*"
    },
    {
      "Sid": "S3ListUserPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "${S3_BUCKET_ARN}",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["${SITE_NAME}/*", ".tools/*", ".skills/*"]
        }
      }
    },
    {
      "Sid": "SQSSendAgentQueue",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "${SQS_QUEUE_ARN}"
    },
    {
      "Sid": "SQSSendIndexingQueue",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "${SQS_INDEXING_QUEUE_ARN}"
    },
    {
      "Sid": "DynamoDBAgentLocksUserScoped",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:DeleteItem"],
      "Resource": "${DDB_TABLE_ARN}",
      "Condition": {
        "ForAllValues:StringEquals": {
          "dynamodb:LeadingKeys": ["${USER_ID}"]
        }
      }
    }
  ]
}
EOF
)

echo "Applying inline policy (idempotent)..."
aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "yoloscribe-user-access" \
  --policy-document "$INLINE_POLICY"

echo "Attaching AmazonBedrockMantleInferenceAccess managed policy..."
aws "${AWS_ARGS[@]}" iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/AmazonBedrockMantleInferenceAccess"

# ── K8s ServiceAccount ─────────────────────────────────────────────────────────

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/yoloscribe/${ROLE_NAME}"

if kubectl get serviceaccount "$SA_NAME" -n "$K8S_NAMESPACE" > /dev/null 2>&1; then
  echo "K8s ServiceAccount '${SA_NAME}' already exists — skipping."
else
  echo "Creating K8s ServiceAccount: ${SA_NAME}"
  kubectl create serviceaccount "$SA_NAME" -n "$K8S_NAMESPACE"
  kubectl annotate serviceaccount "$SA_NAME" -n "$K8S_NAMESPACE" \
    "eks.amazonaws.com/role-arn=${ROLE_ARN}"
fi

echo ""
echo "Done. Role ARN:"
echo "  ${ROLE_ARN}"
