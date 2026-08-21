#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=agent-runner
RELEASE=yoloscribe-agent-runner
CHART="$SCRIPT_DIR/yoloscribe-agent-runner"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env ANTHROPIC_API_KEY GHCR_PAT

helm_upgrade_install \
  --set anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  ${LITELLM_MASTER_KEY:+--set litellmApiKey="$LITELLM_MASTER_KEY"} \
  ${OTEL_EXPORTER_OTLP_ENDPOINT:+--set otel.endpoint="$OTEL_EXPORTER_OTLP_ENDPOINT"} \
  ${OTEL_EXPORTER_OTLP_HEADERS:+--set otel.headers="$OTEL_EXPORTER_OTLP_HEADERS"}
