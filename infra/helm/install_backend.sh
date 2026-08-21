#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=backend
RELEASE=yoloscribe-backend
CHART="$SCRIPT_DIR/yoloscribe-backend"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env ANTHROPIC_API_KEY GHCR_PAT SUPABASE_SERVICE_ROLE_KEY

# YoloBrain signal delivery (YOL-558) needs BOTH values. The chart emits no
# YOLOBRAIN_* env at all when apiUrl is empty, so supplying only one looks
# configured and delivers nothing — warn rather than let that pass silently.
if { [[ -n "${YOLOBRAIN_API_URL:-}" ]] && [[ -z "${YOLOBRAIN_INTERNAL_SECRET:-}" ]]; } ||
   { [[ -z "${YOLOBRAIN_API_URL:-}" ]] && [[ -n "${YOLOBRAIN_INTERNAL_SECRET:-}" ]]; }; then
  echo "Warning: only one of YOLOBRAIN_API_URL / YOLOBRAIN_INTERNAL_SECRET is set."
  echo "         Signal delivery to YoloBrain needs both; it will stay disabled."
fi

helm_upgrade_install \
  --set anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  --set supabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY" \
  ${LITELLM_MASTER_KEY:+--set litellmApiKey="$LITELLM_MASTER_KEY"} \
  ${MESSAGING_BOT_SECRET:+--set messagingBotEnabled=true --set messagingBotSecret="$MESSAGING_BOT_SECRET"} \
  ${OTEL_EXPORTER_OTLP_ENDPOINT:+--set otel.endpoint="$OTEL_EXPORTER_OTLP_ENDPOINT"} \
  ${OTEL_EXPORTER_OTLP_HEADERS:+--set otel.headers="$OTEL_EXPORTER_OTLP_HEADERS"} \
  ${YOLOBRAIN_API_URL:+--set yolobrain.apiUrl="$YOLOBRAIN_API_URL"} \
  ${YOLOBRAIN_INTERNAL_SECRET:+--set yolobrain.internalSecret="$YOLOBRAIN_INTERNAL_SECRET"}
