#!/usr/bin/env bash
# setup_litellm_virtual_keys.sh — Verify LiteLLM virtual-key support for
# YoloScribe token budgets (YOL-513).
#
# LiteLLM stores virtual keys in its Postgres DB and runs its OWN schema
# migrations automatically on proxy startup (when DATABASE_URL is set) — there is
# no separate migration for YoloScribe to run. This script therefore exercises
# the admin API to confirm the proxy can actually mint budgeted virtual keys
# (i.e. the DB is wired and key management is enabled): it generates a throwaway
# probe key, then deletes it. Idempotent — safe to re-run.
#
# YoloScribe's /provision uses the same /key/generate endpoint at runtime to mint
# one budgeted virtual key per user.
#
# Usage:
#   LITELLM_BASE_URL=http://localhost:4000 LITELLM_MASTER_KEY=sk-... \
#     ./infra/scripts/setup_litellm_virtual_keys.sh
#
# For the in-cluster proxy, port-forward first:
#   kubectl port-forward svc/litellm -n yolo 4000:4000
#
# (LITELLM_BASE_URL / LITELLM_MASTER_KEY are also read from the repo-root .env.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

BASE="${LITELLM_BASE_URL:-}"
KEY="${LITELLM_MASTER_KEY:-${LITELLM_API_KEY:-}}"

# Admin endpoints live at the root, not under /v1 — strip a trailing /v1 or slash.
BASE="${BASE%/v1}"
BASE="${BASE%/}"

if [[ -z "$BASE" ]]; then
  echo "Error: LITELLM_BASE_URL is not set (e.g. http://localhost:4000)"
  exit 1
fi
if [[ -z "$KEY" ]]; then
  echo "Error: LITELLM_MASTER_KEY (or LITELLM_API_KEY) is not set"
  exit 1
fi

echo "LiteLLM: $BASE"
echo ""

# ── 1. Reachability ───────────────────────────────────────────────────────────
if ! curl -fsS "$BASE/health/liveliness" >/dev/null 2>&1; then
  echo "✗ LiteLLM not reachable at $BASE (/health/liveliness failed)."
  echo "  Port-forward the in-cluster proxy: kubectl port-forward svc/litellm -n yolo 4000:4000"
  exit 1
fi
echo "✓ Proxy reachable"

# ── 2. Mint a throwaway probe virtual key ─────────────────────────────────────
# Tiny budget + 1-day TTL so it self-expires even if cleanup below is skipped.
echo "→ Generating a probe virtual key via /key/generate ..."
resp="$(curl -sS -X POST "$BASE/key/generate" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"yoloscribe-vk-probe","max_budget":0.01,"budget_duration":"1d","duration":"1d","metadata":{"probe":"yoloscribe-setup"}}' \
  2>&1 || true)"

probe_key="$(printf '%s' "$resp" | python3 -c 'import sys,json
try:
    print(json.load(sys.stdin).get("key",""))
except Exception:
    print("")' 2>/dev/null || true)"

if [[ -z "$probe_key" ]]; then
  echo ""
  echo "✗ /key/generate did not return a key — virtual-key management is unavailable."
  echo "  LiteLLM needs a database for virtual keys. Ensure the proxy is started with"
  echo "  DATABASE_URL set (it runs its own migrations on boot) and key management"
  echo "  enabled, then restart it and re-run this script."
  echo "  Response: $resp"
  exit 1
fi
echo "✓ Virtual key minted — DB + key management are live"

# ── 3. Clean up the probe key ─────────────────────────────────────────────────
if curl -fsS -X POST "$BASE/key/delete" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"keys\":[\"$probe_key\"]}" >/dev/null 2>&1; then
  echo "✓ Probe key deleted"
else
  echo "⚠ Could not delete the probe key (harmless — 0.01 budget, 1-day TTL): $probe_key"
fi

echo ""
echo "LiteLLM virtual keys are ready. YoloScribe /provision can now mint a"
echo "per-user budgeted key against $BASE (YOL-513)."
