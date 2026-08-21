#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Load root .env if present, without permanently polluting the environment
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

# ── Argument parsing ──────────────────────────────────────────────────────────

DRY_RUN=false
EXTRA_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *) EXTRA_ARGS+=("$arg") ;;
  esac
done

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] Helm will render templates but make no changes to the cluster."
  EXTRA_ARGS+=(--dry-run)
fi

if [[ -z "${STAGE:-}" ]]; then
  echo "Error: STAGE is not set (e.g. dev, staging, prod)"
  exit 1
fi

if [[ -z "${REGION:-}" ]]; then
  echo "Error: REGION is not set (e.g. us-west-2)"
  exit 1
fi

VALUES_FILE="$SCRIPT_DIR/backend.${STAGE}.${REGION}.values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE"
  exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Error: ANTHROPIC_API_KEY is not set in environment or .env"
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set in environment or .env"
  exit 1
fi

if [[ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]]; then
  echo "Error: SUPABASE_SERVICE_ROLE_KEY is not set in environment or .env"
  exit 1
fi

# YoloBrain signal delivery (YOL-558) needs BOTH values. The chart emits no
# YOLOBRAIN_* env at all when apiUrl is empty, so supplying only one looks
# configured and delivers nothing — warn rather than let that pass silently.
if { [[ -n "${YOLOBRAIN_API_URL:-}" ]] && [[ -z "${YOLOBRAIN_INTERNAL_SECRET:-}" ]]; } ||
   { [[ -z "${YOLOBRAIN_API_URL:-}" ]] && [[ -n "${YOLOBRAIN_INTERNAL_SECRET:-}" ]]; }; then
  echo "Warning: only one of YOLOBRAIN_API_URL / YOLOBRAIN_INTERNAL_SECRET is set."
  echo "         Signal delivery to YoloBrain needs both; it will stay disabled."
fi

helm upgrade --install yoloscribe-backend \
  "$SCRIPT_DIR/yoloscribe-backend" \
  --namespace yolo \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  --set supabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY" \
  ${LITELLM_MASTER_KEY:+--set litellmApiKey="$LITELLM_MASTER_KEY"} \
  ${MESSAGING_BOT_SECRET:+--set messagingBotEnabled=true --set messagingBotSecret="$MESSAGING_BOT_SECRET"} \
  ${OTEL_EXPORTER_OTLP_ENDPOINT:+--set otel.endpoint="$OTEL_EXPORTER_OTLP_ENDPOINT"} \
  ${OTEL_EXPORTER_OTLP_HEADERS:+--set otel.headers="$OTEL_EXPORTER_OTLP_HEADERS"} \
  ${YOLOBRAIN_API_URL:+--set yolobrain.apiUrl="$YOLOBRAIN_API_URL"} \
  ${YOLOBRAIN_INTERNAL_SECRET:+--set yolobrain.internalSecret="$YOLOBRAIN_INTERNAL_SECRET"} \
  "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
