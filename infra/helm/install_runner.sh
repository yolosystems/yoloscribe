#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../backend/.env"

# Load backend/.env if present, without permanently polluting the environment
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Error: ANTHROPIC_API_KEY is not set in environment or backend/.env"
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set"
  exit 1
fi

if [[ -z "${GITHUB_USER:-}" ]]; then
  echo "Error: GITHUB_USER is not set"
  exit 1
fi

if [[ -z "${SQS_QUEUE_URL:-}" ]]; then
  echo "Error: SQS_QUEUE_URL is not set in environment or backend/.env"
  exit 1
fi

if [[ -z "${AGENT_RUNNER_ROLE_ARN:-}" ]]; then
  echo "Error: AGENT_RUNNER_ROLE_ARN is not set"
  echo "  Run infra/iam/create_agent_runner_role.sh to create it, then:"
  echo "  export AGENT_RUNNER_ROLE_ARN=arn:aws:iam::<account>:role/agentscribe-agent-runner"
  exit 1
fi

helm upgrade --install agentscribe-agent-runner \
  "$SCRIPT_DIR/agentscribe-agent-runner" \
  --namespace yolo \
  --create-namespace \
  --set anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  --set ghcr.username="$GITHUB_USER" \
  --set config.sqsQueueUrl="$SQS_QUEUE_URL" \
  --set serviceAccount.iamRoleArn="$AGENT_RUNNER_ROLE_ARN" \
  "$@"
