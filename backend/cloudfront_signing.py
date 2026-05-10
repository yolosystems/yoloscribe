"""CloudFront signed cookie generation for YoloScribe media assets.

Loads the RSA private key from Secrets Manager (or S3 in local mode) at import
time and exposes a single public function: ``sign_media_cookies``.

CloudFront custom-policy signed cookies require three response cookies:
  CloudFront-Policy      — base64(policy_json)
  CloudFront-Signature   — base64(RSA-SHA1(policy_json))
  CloudFront-Key-Pair-Id — the key pair ID registered in CloudFront

Base64 encoding uses CloudFront's variant: +→-, =→_, /→~
"""

from __future__ import annotations

import base64
import json
import logging
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

# Populated at startup by _load_signing_key().
_private_key = None

_SM_SECRET_NAME = "yoloscribe/cloudfront-signing-key"

# Cookie TTL in seconds (1 hour).
COOKIE_TTL = 3600


def _cf_b64(data: bytes) -> str:
    """CloudFront-safe base64: standard b64 with +→-, =→_, /→~."""
    return base64.b64encode(data).decode().replace("+", "-").replace("=", "_").replace("/", "~")


def load_signing_key(secrets_store) -> None:
    """Load the CloudFront RSA private key from the secrets store.

    Called once at backend startup (from config.py). Logs a warning and leaves
    _private_key as None if the secret is absent — the /media-auth endpoint
    returns 503 in that case rather than crashing the whole process.
    """
    global _private_key
    pem = secrets_store.get(_SM_SECRET_NAME)
    if not pem:
        log.warning(
            "CloudFront signing key not found in secrets store (%s). "
            "GET /media-auth will return 503 until the key is provisioned.",
            _SM_SECRET_NAME,
        )
        return
    try:
        _private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        log.info("CloudFront signing key loaded successfully")
    except Exception as exc:
        log.error("Failed to load CloudFront signing key: %s", exc)


def is_configured() -> bool:
    """Return True if the signing key has been loaded successfully."""
    return _private_key is not None


def sign_media_cookies(
    cloudfront_domain: str,
    site: str,
    key_pair_id: str,
    ttl: int = COOKIE_TTL,
) -> dict[str, str]:
    """Generate the three CloudFront signed cookies for a site's media prefix.

    Returns a dict mapping cookie name → value (the raw value string, without
    attributes — callers set HttpOnly / SameSite / domain etc. themselves).

    Raises RuntimeError if the signing key has not been loaded.
    """
    if _private_key is None:
        raise RuntimeError("CloudFront signing key is not loaded")

    expires_at = int(time.time()) + ttl
    resource = f"https://{cloudfront_domain}/{site}/*"

    policy = json.dumps(
        {
            "Statement": [
                {
                    "Resource": resource,
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expires_at}},
                }
            ]
        },
        separators=(",", ":"),  # compact — no extra whitespace
    )

    policy_b64 = _cf_b64(policy.encode())

    signature = _private_key.sign(policy.encode(), padding.PKCS1v15(), hashes.SHA1())  # noqa: S303
    signature_b64 = _cf_b64(signature)

    return {
        "CloudFront-Policy": policy_b64,
        "CloudFront-Signature": signature_b64,
        "CloudFront-Key-Pair-Id": key_pair_id,
    }
