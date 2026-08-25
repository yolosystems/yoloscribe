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

require_env PHOENIX_SECRET PHOENIX_PG_PASSWORD PHOENIX_ADMIN_PASSWORD

# The Postgres password is set in two places that must agree: the connection
# settings Phoenix dials out with, and the Secret it reads at runtime. The chart
# does not derive one from the other, so a mismatch surfaces as an auth failure
# against RDS rather than as a config error.
helm_upgrade_install \
  --set database.postgres.password="$PHOENIX_PG_PASSWORD" \
  --set auth.secret[0].value="$PHOENIX_SECRET" \
  --set auth.secret[1].value="$PHOENIX_PG_PASSWORD" \
  --set auth.defaultAdminPassword="$PHOENIX_ADMIN_PASSWORD" \
  ${PHOENIX_CHART_VERSION:+--version "$PHOENIX_CHART_VERSION"}
