#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=agent-runner
RELEASE=yoloscribe-agent-runner
CHART="$SCRIPT_DIR/yoloscribe-agent-runner"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# No secrets pass through this script — see install_backend.sh.
helm_upgrade_install
