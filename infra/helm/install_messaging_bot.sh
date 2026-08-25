#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=messaging-bot
RELEASE=yoloscribe-messaging-bot
CHART="$SCRIPT_DIR/yoloscribe-messaging-bot"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env GHCR_PAT

# Required only when the values file does not name an existing Secret, which this
# script cannot see. Under External Secrets the value never passes through here
# at all, so demand it only when it is actually being used.
if [[ -z "${MESSAGING_BOT_SECRET:-}" ]]; then
  echo "Note: MESSAGING_BOT_SECRET is not set. That is correct when the values"
  echo "      file sets existingSecret (External Secrets supplies it); otherwise"
  echo "      the bot will have no secret. Generate one with: openssl rand -hex 32"
fi

# The bot must never be able to mint run tokens (YOL-523). This check only sees
# environment values, so it is dead under External Secrets — the same invariant
# is enforced at write time in infra/scripts/create_deploy_secrets.sh.
if [[ -n "${MESSAGING_BOT_SECRET:-}" && -n "${INTERNAL_MINT_SECRET:-}" \
      && "${MESSAGING_BOT_SECRET}" == "${INTERNAL_MINT_SECRET}" ]]; then
  echo "Error: MESSAGING_BOT_SECRET must not equal INTERNAL_MINT_SECRET." >&2
  echo "The bot processes untrusted chat input, and /internal/runs/mint accepts an" >&2
  echo "arbitrary site + user_id — sharing one value would let a compromised bot" >&2
  echo "mint run tokens for any site. Generate a separate secret." >&2
  exit 1
fi

# Discord token is optional — only required when discord is in ENABLED_ADAPTERS
helm_upgrade_install \
  ${MESSAGING_BOT_SECRET:+--set messagingBotSecret="$MESSAGING_BOT_SECRET"} \
  --set ghcr.pat="$GHCR_PAT" \
  ${DISCORD_BOT_TOKEN:+--set discordBotToken="$DISCORD_BOT_TOKEN"}
