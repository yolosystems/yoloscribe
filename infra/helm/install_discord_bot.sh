#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Load root .env if present, without permanently polluting the environment
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${STAGE:-}" ]]; then
  echo "Error: STAGE is not set (e.g. dev, staging, prod)"
  exit 1
fi

if [[ -z "${REGION:-}" ]]; then
  echo "Error: REGION is not set (e.g. us-west-2)"
  exit 1
fi

VALUES_FILE="$SCRIPT_DIR/discord-bot.${STAGE}.${REGION}.values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE"
  exit 1
fi

if [[ -z "${DISCORD_BOT_TOKEN:-}" ]]; then
  echo "Error: DISCORD_BOT_TOKEN is not set in environment or .env"
  exit 1
fi

if [[ -z "${DISCORD_AES_KEY:-}" ]]; then
  echo "Error: DISCORD_AES_KEY is not set in environment or .env"
  echo "Generate one with: python3 -c \"import os, base64; print(base64.b64encode(os.urandom(32)).decode())\""
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set in environment or .env"
  exit 1
fi

helm upgrade --install agentscribe-discord-bot \
  "$SCRIPT_DIR/agentscribe-discord-bot" \
  --namespace yolo \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set discordBotToken="$DISCORD_BOT_TOKEN" \
  --set discordAesKey="$DISCORD_AES_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  "$@"
