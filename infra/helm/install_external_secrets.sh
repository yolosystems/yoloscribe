#!/usr/bin/env bash
set -euo pipefail

# Sync YoloScribe's deployment secrets from AWS Secrets Manager into the
# Kubernetes Secrets the other charts consume.
#
# Install AFTER the External Secrets Operator and BEFORE any YoloScribe chart:
# a secretKeyRef is a hard requirement, so a pod whose Secret has not synced yet
# fails CreateContainerConfigError rather than waiting for it.
#
# This script passes no secret values. That is the point — see
# infra/iam/README-eso.md for the Secrets Manager objects it expects.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Distinct from YoloBrain's `external-secrets` component name: yoloscribe-ops is
# one flat directory shared by both products, so a name collision there means two
# different charts resolving the same values file — which is how litellm ended up
# with two divergent files for one deployed release.
COMPONENT=yoloscribe-external-secrets
RELEASE=yoloscribe-external-secrets
CHART="$SCRIPT_DIR/yoloscribe-external-secrets"
# shellcheck source=infra/helm/_common.sh
source "$SCRIPT_DIR/_common.sh"

# The CRDs belong to the operator, which may not be installed at render time on
# a first pass; without this Helm rejects manifests whose kinds it cannot
# resolve. Same reason YoloBrain's equivalent carries the flag.
helm_upgrade_install --disable-openapi-validation

cat <<NEXT

Verify the secrets synced before installing anything that consumes them:
  kubectl get externalsecret -n "$K8S_NAMESPACE"
All should report READY=True. A SecretSyncedError usually means either the
Secrets Manager object does not exist, or the operator's IAM role is not
granted yoloscribe/deploy/* — see infra/iam/README-eso.md.
NEXT
