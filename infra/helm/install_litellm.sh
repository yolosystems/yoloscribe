#!/usr/bin/env bash
set -euo pipefail

# Deploy the LiteLLM proxy that carries YoloScribe's model routing, using the
# OFFICIAL berriai/litellm-helm chart + a YoloScribe values file. LiteLLM's
# infrastructure (Postgres DB, provider-credential secrets) is assumed to
# already exist — this only deploys the proxy. See infra/helm/litellm.example.values.yaml.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Load root .env if present, without permanently polluting the environment.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

# Official LiteLLM Helm chart. Pin LITELLM_CHART_VERSION for reproducible deploys.
LITELLM_CHART="${LITELLM_CHART:-oci://ghcr.io/berriai/litellm-helm}"
LITELLM_CHART_VERSION="${LITELLM_CHART_VERSION:-}"

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

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "Error: LITELLM_MASTER_KEY is not set in environment or .env"
  exit 1
fi

VALUES_FILE="$SCRIPT_DIR/litellm.${STAGE}.${REGION}.values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE"
  exit 1
fi

helm upgrade --install yoloscribe-litellm \
  "$LITELLM_CHART" \
  ${LITELLM_CHART_VERSION:+--version "$LITELLM_CHART_VERSION"} \
  --namespace yolo \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set masterkey="$LITELLM_MASTER_KEY" \
  "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
