# shellcheck shell=bash
#
# Shared preamble for the install_*.sh scripts in this directory. Sourced,
# never executed.
#
# The caller names the release it deploys, then sources this file:
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   COMPONENT=backend                       # values-file prefix
#   RELEASE=yoloscribe-backend              # helm release name
#   CHART="$SCRIPT_DIR/yoloscribe-backend"  # chart path, or an OCI ref
#   source "$SCRIPT_DIR/_common.sh"
#
# On return:
#   VALUES_FILE     resolved values file — guaranteed to exist
#   K8S_NAMESPACE   target namespace (required; deliberately no default)
#   DRY_RUN         true|false
#   EXTRA_ARGS[]    unrecognised arguments, plus --dry-run when requested
#
# and two functions are available: require_env, and helm_upgrade_install,
# which takes the component's own --set flags as its arguments.
#
# The contract (YOL-573) is shared with YoloBrain's helm/_common.sh. The two
# repos cannot import from each other, so the files are kept in sync by hand —
# change one, change the other.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Error: _common.sh is sourced by an install_*.sh script, not run directly." >&2
  exit 1
fi

: "${COMPONENT:?_common.sh: COMPONENT must be set before sourcing}"
: "${RELEASE:?_common.sh: RELEASE must be set before sourcing}"
: "${CHART:?_common.sh: CHART must be set before sourcing}"

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$COMMON_DIR/../../.env"

# Load root .env if present, without permanently polluting the environment.
#
# An explicit environment beats .env for the four variables that decide *where*
# this deploys. `source` overwrites whatever is already set, so without the
# snapshot below, `STAGE=prod ./install_backend.sh` would silently deploy to
# whatever STAGE the .env happens to name — and now that the namespace is a
# required input, `K8S_NAMESPACE=other ./install_backend.sh` would ignore it
# too, which would make "required" meaningless. Secrets deliberately still come
# from .env; it is only the routing that the caller can override.
_pre_stage="${STAGE:-}"
_pre_region="${REGION:-}"
_pre_namespace="${K8S_NAMESPACE:-}"
_pre_namespace_alias="${NAMESPACE:-}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

[[ -n "$_pre_stage" ]] && STAGE="$_pre_stage"
[[ -n "$_pre_region" ]] && REGION="$_pre_region"
[[ -n "$_pre_namespace" ]] && K8S_NAMESPACE="$_pre_namespace"
# A NAMESPACE passed on the command line outranks a K8S_NAMESPACE that only
# came from .env — otherwise the alias would look accepted and be ignored.
[[ -z "$_pre_namespace" && -n "$_pre_namespace_alias" ]] && K8S_NAMESPACE="$_pre_namespace_alias"
unset _pre_stage _pre_region _pre_namespace _pre_namespace_alias

# ── Helpers ───────────────────────────────────────────────────────────────────

# Bash tilde-expands a word only when it stands alone, so `--values-dir ~/ops`
# arrives expanded but `--values-dir=~/ops` arrives as a literal `~/ops`. Both
# spellings are natural to type, so put the second one right by hand.
expand_tilde() {
  case "$1" in
    "~")   printf '%s' "$HOME" ;;
    "~/"*) printf '%s' "$HOME/${1#\~/}" ;;
    *)     printf '%s' "$1" ;;
  esac
}

require_env() {
  local missing=() var
  for var in "$@"; do
    [[ -n "${!var:-}" ]] || missing+=("$var")
  done
  if (( ${#missing[@]} )); then
    for var in "${missing[@]}"; do
      echo "Error: $var is not set in environment or .env" >&2
    done
    exit 1
  fi
}

helm_upgrade_install() {
  echo "==> helm upgrade --install"
  echo "    release:    $RELEASE"
  echo "    chart:      $CHART"
  echo "    namespace:  $K8S_NAMESPACE"
  echo "    values:     $VALUES_FILE"
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "    mode:       dry-run — templates render, the cluster is untouched"
  fi
  echo

  helm upgrade --install "$RELEASE" \
    "$CHART" \
    --namespace "$K8S_NAMESPACE" \
    --create-namespace \
    --values "$VALUES_FILE" \
    "$@" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
}

# ── Argument parsing ──────────────────────────────────────────────────────────

DRY_RUN=false
VALUES_DIR=""
VALUES_DIR_EXPLICIT=false
EXTRA_ARGS=()

while (( $# )); do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      ;;
    --values-dir)
      [[ $# -ge 2 ]] || { echo "Error: --values-dir requires a path" >&2; exit 1; }
      VALUES_DIR="$2"
      VALUES_DIR_EXPLICIT=true
      shift
      ;;
    --values-dir=*)
      VALUES_DIR="${1#*=}"
      VALUES_DIR_EXPLICIT=true
      ;;
    *)
      EXTRA_ARGS+=("$1")
      ;;
  esac
  shift
done

[[ "$DRY_RUN" == "true" ]] && EXTRA_ARGS+=(--dry-run)

# ── Required inputs ───────────────────────────────────────────────────────────

if [[ -z "${STAGE:-}" ]]; then
  echo "Error: STAGE is not set (e.g. dev, staging, prod)" >&2
  exit 1
fi

if [[ -z "${REGION:-}" ]]; then
  echo "Error: REGION is not set (e.g. us-west-2)" >&2
  exit 1
fi

# NAMESPACE is accepted as an alias, but K8S_NAMESPACE is the name of record:
# it is what YoloBrain's scripts use and what CLAUDE.md documents, and these
# scripts source the repo-root .env, where a variable called NAMESPACE is
# generic enough to collide with something unrelated.
if [[ -z "${K8S_NAMESPACE:-}" && -n "${NAMESPACE:-}" ]]; then
  K8S_NAMESPACE="$NAMESPACE"
fi

if [[ -z "${K8S_NAMESPACE:-}" ]]; then
  # No default, deliberately. Combined with --create-namespace, a defaulted
  # namespace turns a forgotten variable into a silent second copy of the stack
  # in a namespace nobody meant to create — pointed at the same database, with
  # nothing anywhere reporting a failure.
  echo "Error: K8S_NAMESPACE is not set, and there is deliberately no default." >&2
  echo "       e.g. K8S_NAMESPACE=yolo $(basename "$0")" >&2
  exit 1
fi

# ── Values file ───────────────────────────────────────────────────────────────

if [[ -n "$VALUES_DIR" ]]; then
  VALUES_DIR="$(expand_tilde "$VALUES_DIR")"
else
  VALUES_DIR="$COMMON_DIR"
fi

VALUES_FILE="$VALUES_DIR/${COMPONENT}.${STAGE}.${REGION}.values.yaml"

if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE" >&2
  if [[ "$VALUES_DIR_EXPLICIT" == "true" ]]; then
    # Strict on purpose. This directory holds gitignored copies carrying the
    # same filenames as yoloscribe-ops, so a fallback would leave "which file
    # did this actually deploy?" unanswerable after the fact.
    echo "       --values-dir was given, so there is no fallback to $COMMON_DIR" >&2
  else
    echo "       Copy ${COMPONENT}.example.values.yaml and fill it in, or pass" >&2
    echo "       --values-dir <path> to read it from yoloscribe-ops." >&2
  fi
  exit 1
fi
