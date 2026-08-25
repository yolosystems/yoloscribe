#!/usr/bin/env bash
set -euo pipefail

# Create or update the AWS Secrets Manager objects that External Secrets syncs
# into the Kubernetes Secrets the YoloScribe charts consume, using values from
# the repo-root .env.
#
#   infra/scripts/create_deploy_secrets.sh --dry-run   # show what would change
#   infra/scripts/create_deploy_secrets.sh             # create / update
#
# Idempotent: creates the object if absent, puts a new version if present.
#
# Secret VALUES are never printed, not even under --dry-run, and are passed to
# the AWS CLI through a 0600 temp file rather than argv so they do not appear
# in `ps` output. Only key names and value lengths are shown.
#
# The prefix is yoloscribe/deploy/ — deliberately not yoloscribe/, which is the
# runtime per-user namespace. See infra/iam/README-eso.md.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

AWS_PROFILE="${AWS_PROFILE:-runyolo_admin}"
PREFIX="${SECRETS_PREFIX:-yoloscribe/deploy}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
else
  echo "Error: $ENV_FILE not found — this script reads its values from there." >&2
  exit 1
fi

REGION="${REGION:-${AWS_REGION:-us-west-2}}"
AWS=(aws --profile "$AWS_PROFILE" --region "$REGION")

command -v jq >/dev/null || { echo "Error: jq is required" >&2; exit 1; }

# ── Build one JSON object per Secrets Manager entry ───────────────────────────
#
# Keys are included only when their source variable is set, which mirrors how
# the charts wire them: the backend reads supabase-service-role-key only under
# authProvider: supabase, messaging-bot-secret only when the bot is enabled, and
# so on. A key the deployment reads but the object lacks stops the pod at
# CreateContainerConfigError, so over-including is as wrong as under-including.

# Objects are declared as key=ENV_VAR pairs so that a missing source variable can
# be *named* rather than silently dropped. Silence is how you get a pod stuck at
# CreateContainerConfigError three steps later.
declare -a NAMES=() BODIES=() OMITTED=()

add_object() {
  local name="$1"; shift
  local body='{}' omitted='' pair key var val
  for pair in "$@"; do
    key="${pair%%=*}"; var="${pair#*=}"; val="${!var:-}"
    if [[ -z "$val" ]]; then
      omitted+="${omitted:+, }$key (\$$var unset)"
      continue
    fi
    body="$(jq -n --argjson o "$body" --arg k "$key" --arg v "$val" '$o + {($k): $v}')"
  done
  NAMES+=("$name"); BODIES+=("$body"); OMITTED+=("$omitted")
}

add_object "$PREFIX/backend" \
  anthropic-api-key=ANTHROPIC_API_KEY \
  supabase-service-role-key=SUPABASE_SERVICE_ROLE_KEY \
  litellm-api-key=LITELLM_MASTER_KEY \
  messaging-bot-secret=MESSAGING_BOT_SECRET \
  yolobrain-internal-secret=YOLOBRAIN_INTERNAL_SECRET

add_object "$PREFIX/agent-runner" \
  anthropic-api-key=ANTHROPIC_API_KEY \
  litellm-api-key=LITELLM_MASTER_KEY

add_object "$PREFIX/otel" \
  otlp-headers=OTEL_EXPORTER_OTLP_HEADERS

add_object "$PREFIX/ghcr" \
  username=GHCR_USERNAME \
  pat=GHCR_PAT

# ── Report, then apply ────────────────────────────────────────────────────────

echo "Region:  $REGION"
echo "Profile: $AWS_PROFILE"
echo "Prefix:  $PREFIX"
echo

FAILED=0
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  body="${BODIES[$i]}"
  count="$(jq 'length' <<<"$body")"

  if (( count == 0 )); then
    echo "  SKIP   $name — no source variables set"
    continue
  fi

  # Key names and value lengths only; never the values themselves.
  summary="$(jq -r 'to_entries | map("\(.key)(\(.value | length))") | join(", ")' <<<"$body")"
  omitted="${OMITTED[$i]}"

  if "${AWS[@]}" secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
    action="update"
  else
    action="create"
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    printf '  %-6s %s\n         keys:    %s\n' "[$action]" "$name" "$summary"
    [[ -n "$omitted" ]] && printf '         omitted: %s\n' "$omitted"
    continue
  fi

  # Pass the body through a 0600 temp file rather than argv, so the value never
  # shows up in `ps` for anyone else on the host.
  tmp="$(mktemp)"; chmod 600 "$tmp"; printf '%s' "$body" > "$tmp"
  if [[ "$action" == "create" ]]; then
    "${AWS[@]}" secretsmanager create-secret \
      --name "$name" \
      --description "YoloScribe deployment secret, synced by External Secrets" \
      --secret-string "file://$tmp" >/dev/null || { FAILED=1; echo "  ERROR  $name"; rm -f "$tmp"; continue; }
  else
    "${AWS[@]}" secretsmanager put-secret-value \
      --secret-id "$name" \
      --secret-string "file://$tmp" >/dev/null || { FAILED=1; echo "  ERROR  $name"; rm -f "$tmp"; continue; }
  fi
  rm -f "$tmp"
  printf '  %-6s %s\n         keys:    %s\n' "[$action]" "$name" "$summary"
  [[ -n "$omitted" ]] && printf '         omitted: %s\n' "$omitted"
done

echo
if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry run — nothing was written."
  exit 0
fi
(( FAILED == 0 )) || { echo "One or more secrets failed."; exit 1; }

cat <<NEXT
Done. Next:
  1. Attach the read policy to the External Secrets role, if not already:
       infra/iam/yoloscribe-eso-policy.json  (see infra/iam/README-eso.md)
  2. Install the sync release:
       infra/helm/install_external_secrets.sh --values-dir <ops>/helm
  3. Confirm every ExternalSecret reports READY=True before installing anything
     that consumes them:
       kubectl get externalsecret -n \${K8S_NAMESPACE:-yolo}

Rotating a value later: re-run this script, then restart the consumers —
External Secrets updates the Kubernetes Secret in place but does not restart
pods, so a running process keeps the old value until it rolls.
NEXT
