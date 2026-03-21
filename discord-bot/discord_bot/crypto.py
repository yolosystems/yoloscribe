"""AES-256-GCM encryption helpers for storing API tokens in discord_configs.

The encrypted_token column stores a base64-encoded blob of:
    nonce (12 bytes) || ciphertext+tag

The plaintext is a JSON payload: {"token": "as_...", "site_name": "..."}
Bundling both values avoids needing an extra Supabase lookup on every message.
"""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key() -> bytes:
    from discord_bot.config import DISCORD_AES_KEY
    return base64.b64decode(DISCORD_AES_KEY)


def encrypt_payload(token: str, site_name: str) -> str:
    """Encrypt token + site_name as AES-256-GCM. Returns base64(nonce||ciphertext+tag)."""
    plaintext = json.dumps({"token": token, "site_name": site_name}).encode()
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct).decode()


def decrypt_payload(encrypted: str) -> tuple[str, str]:
    """Decrypt an encrypted_token blob. Returns (raw_token, site_name)."""
    data = base64.b64decode(encrypted)
    nonce, ct = data[:12], data[12:]
    payload = json.loads(AESGCM(_key()).decrypt(nonce, ct, None))
    return payload["token"], payload["site_name"]
