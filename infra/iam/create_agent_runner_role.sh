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
: "${STAGE:?STAGE must be set: policy grants are scoped to it}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
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

# The policy file is a template: placeholders keep the grants scoped to this
# stage's resources rather than every yoloscribe-* in the account, so a dev role
# cannot reach prod's bucket, tables or queues.
POLICY_DOCUMENT=$(sed \
  -e "s|__AWS_REGION__|${AWS_REGION}|g" \
  -e "s|__AWS_ACCOUNT_ID__|${AWS_ACCOUNT_ID}|g" \
  -e "s|__STAGE__|${STAGE}|g" \
  -e "s|__S3_BUCKET__|${S3_BUCKET}|g" \
  "$POLICY_FILE")

if printf '%s' "$POLICY_DOCUMENT" | grep -q '__[A-Z_]*__'; then
  echo "Error: unsubstituted placeholders remain in the policy:" >&2
  printf '%s' "$POLICY_DOCUMENT" | grep -o '__[A-Z_]*__' | sort -u | sed 's/^/  /' >&2
  exit 1
fi

echo "Applying inline policy (idempotent)..."
aws "${AWS_ARGS[@]}" iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "yoloscribe-agent-runner-access" \
  --policy-document "$POLICY_DOCUMENT"

# No Bedrock managed policy is attached: all model inference routes through the
# LiteLLM proxy (YOL-512), whose own IRSA role carries the inference permissions.
# The only direct Bedrock use here is embeddings for semantic search, granted by
# the BedrockEmbed statement in the inline policy above.

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Done. Role ARN:"
echo "  $ROLE_ARN"
echo ""
echo "Set this in your environment before running install_runner.sh:"
echo "  export AGENT_RUNNER_ROLE_ARN=$ROLE_ARN"
