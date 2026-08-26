#!/usr/bin/env bash
set -euo pipefail

# Deploy Arize Phoenix (OTEL tracing + eval annotation) from the OFFICIAL vendor
# Helm chart plus an environment values file — the same shape as install_litellm.sh.
#
# Phoenix REQUIRES an external Postgres on the shared RDS instance; the chart's
# bundled Postgres and its SQLite fallback are both disabled in the values file.
# See infra/helm/phoenix.example.values.yaml and yoloscribe-ops/CLUSTER.md.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Official Phoenix Helm chart. Pin PHOENIX_CHART_VERSION for reproducible deploys.
PHOENIX_CHART="${PHOENIX_CHART:-oci://registry-1.docker.io/arizephoenix/phoenix-helm}"
PHOENIX_CHART_VERSION="${PHOENIX_CHART_VERSION:-}"

COMPONENT=phoenix
RELEASE=phoenix
CHART="$PHOENIX_CHART"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# No secrets pass through this script. The vendor chart reads PHOENIX_SECRET and
# PHOENIX_POSTGRES_PASSWORD from the `phoenix-secret` Secret, which it does not
# create (auth.createSecret: false) — External Secrets syncs it instead.
#
# auth.defaultAdminPassword used to be passed here too. With createSecret false
# it renders nowhere, so it was a no-op.
helm_upgrade_install ${PHOENIX_CHART_VERSION:+--version "$PHOENIX_CHART_VERSION"}
