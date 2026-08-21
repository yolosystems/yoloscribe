#!/usr/bin/env bash
set -euo pipefail

# Deploy the LiteLLM proxy that carries YoloScribe's model routing, using the
# OFFICIAL berriai/litellm-helm chart + a YoloScribe values file. LiteLLM's
# infrastructure (Postgres DB, provider-credential secrets) is assumed to
# already exist — this only deploys the proxy. See infra/helm/litellm.example.values.yaml.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Official LiteLLM Helm chart. Pin LITELLM_CHART_VERSION for reproducible deploys.
LITELLM_CHART="${LITELLM_CHART:-oci://ghcr.io/berriai/litellm-helm}"
LITELLM_CHART_VERSION="${LITELLM_CHART_VERSION:-}"

COMPONENT=litellm
RELEASE=yoloscribe-litellm
CHART="$LITELLM_CHART"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env LITELLM_MASTER_KEY

helm_upgrade_install \
  ${LITELLM_CHART_VERSION:+--version "$LITELLM_CHART_VERSION"} \
  --set masterkey="$LITELLM_MASTER_KEY"
