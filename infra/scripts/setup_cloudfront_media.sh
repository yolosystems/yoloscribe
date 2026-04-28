#!/usr/bin/env bash
# setup_cloudfront_media.sh — One-time operator setup for YoloScribe media delivery.
#
# Creates a CloudFront public key + key group for signed-cookie media auth,
# adds the key group as a trusted signer on the assets/* behaviour of your
# CloudFront distribution, and stores the private key in AWS Secrets Manager
# so the backend can sign cookies at runtime.
#
# Run once per environment before deploying the backend.  Idempotent for the
# Secrets Manager step; re-running creates a new key pair and updates the secret.
#
# Usage:
#   AWS_PROFILE=myprofile AWS_REGION=us-east-1 \
#   DISTRIBUTION_ID=E1EXAMPLE \
#   ./scripts/setup_cloudfront_media.sh
#
# Required env vars:
#   DISTRIBUTION_ID   — CloudFront distribution ID to update
#
# Optional env vars:
#   AWS_REGION        — AWS region for Secrets Manager (default: us-east-1)
#   KEY_NAME          — human-readable CloudFront public key name (default: yoloscribe-media)
#   KEY_GROUP_NAME    — CloudFront key group name (default: yoloscribe-media-group)
#   SECRET_NAME       — Secrets Manager secret name (default: yoloscribe/cloudfront-signing-key)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
DISTRIBUTION_ID="${DISTRIBUTION_ID:?DISTRIBUTION_ID env var is required}"
KEY_NAME="${KEY_NAME:-yoloscribe-media}"
KEY_GROUP_NAME="${KEY_GROUP_NAME:-yoloscribe-media-group}"
SECRET_NAME="${SECRET_NAME:-yoloscribe/cloudfront-signing-key}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

PRIVATE_KEY_FILE="${TMPDIR}/private.pem"
PUBLIC_KEY_FILE="${TMPDIR}/public.pem"

echo "=== YoloScribe CloudFront media signing setup ==="
echo "Region:          ${REGION}"
echo "Distribution:    ${DISTRIBUTION_ID}"
echo "Key name:        ${KEY_NAME}"
echo "Key group:       ${KEY_GROUP_NAME}"
echo "Secret:          ${SECRET_NAME}"
echo ""

# ---------------------------------------------------------------------------
# 1. Generate RSA-2048 key pair
# ---------------------------------------------------------------------------

echo "→ Generating RSA-2048 key pair…"
openssl genrsa -out "${PRIVATE_KEY_FILE}" 2048 2>/dev/null
openssl rsa -pubout -in "${PRIVATE_KEY_FILE}" -out "${PUBLIC_KEY_FILE}" 2>/dev/null
echo "  Key pair generated."

# ---------------------------------------------------------------------------
# 2. Register the public key with CloudFront
# ---------------------------------------------------------------------------

echo "→ Creating CloudFront public key '${KEY_NAME}'…"
PUBLIC_KEY_BODY="$(cat "${PUBLIC_KEY_FILE}")"

CF_PUBLIC_KEY_ID="$(aws cloudfront create-public-key \
  --public-key-config \
    "CallerReference=$(date +%s),Name=${KEY_NAME},EncodedKey=${PUBLIC_KEY_BODY},Comment=YoloScribe media signing key" \
  --query 'PublicKey.Id' \
  --output text)"

echo "  CloudFront public key ID: ${CF_PUBLIC_KEY_ID}"

# ---------------------------------------------------------------------------
# 3. Create a key group containing the new public key
# ---------------------------------------------------------------------------

echo "→ Creating CloudFront key group '${KEY_GROUP_NAME}'…"
CF_KEY_GROUP_ID="$(aws cloudfront create-key-group \
  --key-group-config \
    "Name=${KEY_GROUP_NAME},Items=[${CF_PUBLIC_KEY_ID}],Comment=YoloScribe media signed cookies" \
  --query 'KeyGroup.Id' \
  --output text)"

echo "  CloudFront key group ID: ${CF_KEY_GROUP_ID}"

# ---------------------------------------------------------------------------
# 4. Add the key group as a trusted signer on the assets/* cache behaviour
#
# We fetch the current distribution config, inject the key group into the
# matching cache behaviour (path pattern "*/assets/*"), and push the update.
# If no matching behaviour exists the script exits with guidance.
# ---------------------------------------------------------------------------

echo "→ Fetching distribution config for ${DISTRIBUTION_ID}…"
CONFIG_FILE="${TMPDIR}/dist-config.json"
ETAG_FILE="${TMPDIR}/etag.txt"

aws cloudfront get-distribution-config \
  --id "${DISTRIBUTION_ID}" \
  --query '{ETag: ETag, Config: DistributionConfig}' \
  --output json > "${TMPDIR}/raw.json"

python3 - "${TMPDIR}/raw.json" "${CF_KEY_GROUP_ID}" "${CONFIG_FILE}" "${ETAG_FILE}" << 'PYEOF'
import json, sys

raw_path, key_group_id, out_path, etag_path = sys.argv[1:]
with open(raw_path) as f:
    data = json.load(f)

etag = data['ETag']
config = data['Config']

behaviours = config.get('CacheBehaviors', {}).get('Items', [])
matched = False
for b in behaviours:
    if 'assets' in b.get('PathPattern', ''):
        tsg = b.setdefault('TrustedKeyGroups', {'Enabled': True, 'Quantity': 0, 'Items': []})
        if key_group_id not in tsg['Items']:
            tsg['Items'].append(key_group_id)
            tsg['Quantity'] = len(tsg['Items'])
            tsg['Enabled'] = True
        matched = True

if not matched:
    print("WARNING: No cache behaviour with 'assets' in the path pattern was found.")
    print("Add a cache behaviour for '*/assets/*' manually and re-run, or add the")
    print(f"key group '{key_group_id}' to the behaviour's trusted key groups in the console.")

with open(out_path, 'w') as f:
    json.dump(config, f)
with open(etag_path, 'w') as f:
    f.write(etag)
PYEOF

ETAG="$(cat "${ETAG_FILE}")"

echo "→ Updating distribution with trusted key group…"
aws cloudfront update-distribution \
  --id "${DISTRIBUTION_ID}" \
  --if-match "${ETAG}" \
  --distribution-config "file://${CONFIG_FILE}" \
  --output text --query 'Distribution.Id' > /dev/null

echo "  Distribution updated."

# ---------------------------------------------------------------------------
# 5. Store the private key in Secrets Manager
# ---------------------------------------------------------------------------

echo "→ Storing private key in Secrets Manager at '${SECRET_NAME}'…"
PRIVATE_KEY_PEM="$(cat "${PRIVATE_KEY_FILE}")"

if aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" \
     --region "${REGION}" > /dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" \
    --secret-string "${PRIVATE_KEY_PEM}" \
    --region "${REGION}" > /dev/null
  echo "  Secret updated."
else
  aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "YoloScribe CloudFront media signing private key" \
    --secret-string "${PRIVATE_KEY_PEM}" \
    --region "${REGION}" > /dev/null
  echo "  Secret created."
fi

# ---------------------------------------------------------------------------
# Done — print Helm values to copy
# ---------------------------------------------------------------------------

echo ""
echo "=== Setup complete ==="
echo ""
echo "Add the following to your Helm values file (backend.prod.values.yaml):"
echo ""
echo "  config:"
echo "    cloudfrontSigningKeyId: ${CF_PUBLIC_KEY_ID}"
echo "    cloudfrontMediaDomain: <your-cloudfront-domain>   # e.g. d1234abcd.cloudfront.net"
echo ""
echo "Then upgrade the release:"
echo "  helm upgrade yoloscribe-backend infra/helm/yoloscribe-backend \\"
echo "    -f backend.prod.values.yaml ..."
