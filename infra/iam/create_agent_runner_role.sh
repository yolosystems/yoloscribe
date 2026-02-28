#!/usr/bin/env bash
# One-time setup: create the IAM role for the agentscribe-agent-runner
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

ROLE_NAME="agentscribe-agent-runner"
SA_NAME="agentscribe-agent-runner"
POLICY_FILE="$(dirname "${BASH_SOURCE[0]}")/agentscribe-agent-runner-policy.json"

AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

echo "Creating IAM role: $ROLE_NAME"

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

aws "${AWS_ARGS[@]}" iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --description "IRSA role for agentscribe agent-runner polling worker" \
  --output json | jq -r '.Role.Arn'

echo "Attaching inline policy..."
aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "agentscribe-agent-runner-access" \
  --policy-document "file://$POLICY_FILE"

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Done. Role ARN:"
echo "  $ROLE_ARN"
echo ""
echo "Set this in your environment before running install_runner.sh:"
echo "  export AGENT_RUNNER_ROLE_ARN=$ROLE_ARN"
