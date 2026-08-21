#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=messaging-bot
RELEASE=yoloscribe-messaging-bot
CHART="$SCRIPT_DIR/yoloscribe-messaging-bot"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

require_env GHCR_PAT

if [[ -z "${MESSAGING_BOT_SECRET:-}" ]]; then
  echo "Error: MESSAGING_BOT_SECRET is not set in environment or .env" >&2
  echo "Generate one with: openssl rand -hex 32" >&2
  echo "The same value must be passed to the backend (messagingBotEnabled=true," >&2
  echo "messagingBotSecret=...). Do NOT reuse INTERNAL_MINT_SECRET." >&2
  exit 1
fi

if [[ -n "${INTERNAL_MINT_SECRET:-}" && "${MESSAGING_BOT_SECRET}" == "${INTERNAL_MINT_SECRET}" ]]; then
  echo "Error: MESSAGING_BOT_SECRET must not equal INTERNAL_MINT_SECRET." >&2
  echo "The bot processes untrusted chat input, and /internal/runs/mint accepts an" >&2
  echo "arbitrary site + user_id — sharing one value would let a compromised bot" >&2
  echo "mint run tokens for any site. Generate a separate secret." >&2
  exit 1
fi

# Discord token is optional — only required when discord is in ENABLED_ADAPTERS
helm_upgrade_install \
  --set messagingBotSecret="$MESSAGING_BOT_SECRET" \
  --set ghcr.pat="$GHCR_PAT" \
  ${DISCORD_BOT_TOKEN:+--set discordBotToken="$DISCORD_BOT_TOKEN"}
