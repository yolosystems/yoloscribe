#!/usr/bin/env bash
set -euo pipefail

# Deploy the LiteLLM proxy from the OFFICIAL vendor chart plus one shared values
# file. Identical to yolobrain/helm/install_litellm.sh apart from the shellcheck
# path hint — both products install the same release from the same values, the
# way Phoenix does.
#
# There was a bundled fork of this chart in yolobrain/helm/litellm until the
# YOL-567 cutover. It exists only in git history now; do not reintroduce one.
#
# Passes no secrets. The master key comes from the Kubernetes Secret named by
# masterkeySecretName in the values file, synced from Secrets Manager.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pin for reproducible deploys; floating means an upgrade can arrive unannounced.
LITELLM_CHART="${LITELLM_CHART:-oci://ghcr.io/berriai/litellm-helm}"
LITELLM_CHART_VERSION="${LITELLM_CHART_VERSION:-}"

COMPONENT=litellm
# The release name is load-bearing: the Service is addressed as `litellm` over
# cluster DNS by Honcho and the YoloScribe backend. A different name here would
# create a SECOND release rather than upgrading this one.
RELEASE=litellm
CHART="$LITELLM_CHART"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

helm_upgrade_install ${LITELLM_CHART_VERSION:+--version "$LITELLM_CHART_VERSION"}
