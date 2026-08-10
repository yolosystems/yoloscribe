#!/usr/bin/env bash
set -euo pipefail

# Create (or update) the AWS WAFv2 WebACL that fronts the public LiteLLM ALB.
#
# Default-DENY: only the browser MCP tool-OAuth handshake is allowed through the
# ingress; all inference/admin/management is reached over internal K8s DNS and is
# blocked at the edge. Rules live in litellm-ingress-web-acl.json (this dir).
#
# Attach it to the ALB by adding the printed WebACL ARN to the LiteLLM ingress
# annotation  alb.ingress.kubernetes.io/wafv2-acl-arn  (see README.md) — let the
# AWS Load Balancer Controller own the association; do NOT associate manually too.
#
# Usage:
#   REGION=us-west-2 ./install_litellm_waf.sh            # create or update
#   REGION=us-west-2 ./install_litellm_waf.sh --dry-run  # print the rendered config only
#
# Requires: aws CLI v2, jq.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACL_JSON="$SCRIPT_DIR/litellm-ingress-web-acl.json"

AWS_PROFILE="${AWS_PROFILE:-runyolo_admin}"
REGION="${REGION:-us-west-2}"
NAME="$(jq -r '.Name' "$ACL_JSON")"
SCOPE="REGIONAL"
AWS=(aws --profile "$AWS_PROFILE" --region "$REGION")

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "[dry-run] WebACL '$NAME' (scope=$SCOPE, region=$REGION) would be created/updated from:"
  echo "          $ACL_JSON"
  jq '.' "$ACL_JSON"
  exit 0
fi

command -v jq >/dev/null || { echo "Error: jq is required"; exit 1; }

# WAF caps Description at 256 chars and forbids some punctuation (no parentheses
# or '*'); fail early with a clear message instead of an opaque ValidationException.
DESC="$(jq -r '.Description // ""' "$ACL_JSON")"
if (( ${#DESC} > 256 )); then
  echo "Error: WebACL Description is ${#DESC} chars (max 256). Shorten it in $ACL_JSON."
  exit 1
fi
if [[ "$DESC" == *"("* || "$DESC" == *")"* || "$DESC" == *"*"* ]]; then
  echo "Error: WebACL Description contains a disallowed character ( ) or *. Fix it in $ACL_JSON."
  exit 1
fi

echo "Looking up existing WebACL '$NAME' in $REGION ..."
EXISTING="$("${AWS[@]}" wafv2 list-web-acls --scope "$SCOPE" \
  --query "WebACLs[?Name=='$NAME'].[Id,LockToken]" --output text || true)"

if [[ -n "$EXISTING" ]]; then
  ID="$(echo "$EXISTING" | awk '{print $1}')"
  LOCK="$(echo "$EXISTING" | awk '{print $2}')"
  echo "Found existing WebACL (id=$ID). Updating rules ..."
  "${AWS[@]}" wafv2 update-web-acl \
    --name "$NAME" \
    --scope "$SCOPE" \
    --id "$ID" \
    --lock-token "$LOCK" \
    --default-action "$(jq -c '.DefaultAction' "$ACL_JSON")" \
    --rules "$(jq -c '.Rules' "$ACL_JSON")" \
    --visibility-config "$(jq -c '.VisibilityConfig' "$ACL_JSON")" \
    >/dev/null
  echo "Updated."
else
  echo "No existing WebACL. Creating ..."
  "${AWS[@]}" wafv2 create-web-acl --cli-input-json "file://$ACL_JSON" >/dev/null
  echo "Created."
fi

ARN="$("${AWS[@]}" wafv2 list-web-acls --scope "$SCOPE" \
  --query "WebACLs[?Name=='$NAME'].ARN" --output text)"

echo
echo "WebACL ARN:"
echo "  $ARN"
echo
echo "Next: attach it to the LiteLLM ingress (see infra/waf/README.md):"
echo "  alb.ingress.kubernetes.io/wafv2-acl-arn: $ARN"
