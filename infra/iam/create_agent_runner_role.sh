#!/usr/bin/env bash
# One-time setup: create the IAM role for the yoloscribe-agent-runner
# polling worker and print its ARN for use in values.yaml / install_runner.sh.
#
# Prerequisites:
#   - AWS CLI configured with credentials that can create IAM roles
#   - The EKS cluster's OIDC provider URL (without https://)
#
# Usage:
#   EKS_OIDC_PROVIDER=oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633 \
#   AWS_ACCOUNT_ID=123456789012 \
#   K8S_NAMESPACE=yolo \
#   AWS_REGION=us-west-2 \
#   AWS_PROFILE=myprofile \
#   bash create_agent_runner_role.sh

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
: "${K8S_NAMESPACE:?K8S_NAMESPACE must be set}"
: "${AWS_REGION:=us-west-2}"

ROLE_NAME="yoloscribe-agent-runner"
SA_NAME="yoloscribe-agent-runner"
POLICY_FILE="$(dirname "${BASH_SOURCE[0]}")/yoloscribe-agent-runner-policy.json"

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

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
  echo "IAM role '$ROLE_NAME' already exists — updating inline policy only."
else
  echo "Creating IAM role: $ROLE_NAME"
  aws "${AWS_ARGS[@]}" iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "IRSA role for yoloscribe agent-runner polling worker" \
    --output json | jq -r '.Role.Arn'
fi

echo "Applying inline policy (idempotent)..."
aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "yoloscribe-agent-runner-access" \
  --policy-document "file://$POLICY_FILE"

# Required for Bedrock Mantle inference (GLM and other Mantle-hosted models).
echo "Attaching AmazonBedrockMantleInferenceAccess managed policy..."
aws "${AWS_ARGS[@]}" iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/AmazonBedrockMantleInferenceAccess"

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Done. Role ARN:"
echo "  $ROLE_ARN"
echo ""
echo "Set this in your environment before running install_runner.sh:"
echo "  export AGENT_RUNNER_ROLE_ARN=$ROLE_ARN"
