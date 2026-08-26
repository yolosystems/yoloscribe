#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=backend
RELEASE=yoloscribe-backend
CHART="$SCRIPT_DIR/yoloscribe-backend"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# No secrets pass through this script. Everything the chart needs is named in the
# values file and synced from Secrets Manager by the yoloscribe-external-secrets
# release — see infra/iam/README-eso.md.
#
# messagingBotEnabled, otel.endpoint and yolobrain.apiUrl were also passed here
# from .env; they are plain config, so they live in the values file now. Setting
# them from a shell meant the deployed configuration existed in no file.
helm_upgrade_install
