#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=indexer
RELEASE=yoloscribe-indexer
CHART="$SCRIPT_DIR/yoloscribe-indexer"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env GHCR_PAT

# No Supabase key: the indexer's secret template was removed when user_id moved
# into the SQS message payload, so requiring one was vestigial.
helm_upgrade_install \
  --set ghcr.pat="$GHCR_PAT"
