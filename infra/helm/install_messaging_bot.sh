#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=messaging-bot
RELEASE=yoloscribe-messaging-bot
CHART="$SCRIPT_DIR/yoloscribe-messaging-bot"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# No secrets pass through this script. The bot-must-not-mint invariant (YOL-523)
# is enforced where the values are written, in infra/scripts/create_deploy_secrets.sh:
# this script can no longer see either value to compare them.
helm_upgrade_install
