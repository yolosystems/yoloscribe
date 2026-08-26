#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=indexer
RELEASE=yoloscribe-indexer
CHART="$SCRIPT_DIR/yoloscribe-indexer"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# No secrets pass through this script — see install_backend.sh.
helm_upgrade_install
