"""Unit tests for the generic OIDC auth provider (Item 3).

Exercises OidcAuthProvider.decode_jwt with a real RS256 signature but a faked
JWKS/discovery step, so no network is touched. Verifies audience + issuer
enforcement and JWTClaims extraction — the behavior the MCP middleware and REST
auth rely on.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from auth_providers.oidc import OidcAuthProvider

# cryptography ships with pyjwt[crypto]; skip cleanly if somehow absent.
crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_ISSUER = "https://issuer.example.com"


@pytest.fixture(scope="module")
def keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return priv_pem, private_key.public_key()


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJwks:
    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._key)


def _provider(public_key, *, client_id="", audience=""):
    p = OidcAuthProvider(
        f"{_ISSUER}/.well-known/openid-configuration",
        client_id=client_id,
        audience=audience,
    )
    # Bypass network discovery.
    p._jwks = _FakeJwks(public_key)
    p._issuer = _ISSUER
    p._discovered = True
    return p


def _sign(priv_pem, claims):
    return pyjwt.encode(claims, priv_pem, algorithm="RS256")


def test_valid_token_returns_claims(keys):
    priv, pub = keys
    p = _provider(pub, audience="my-api")
    token = _sign(priv, {"sub": "user-1", "email": "a@b.com", "aud": "my-api", "iss": _ISSUER})
    claims = p.decode_jwt(token)
    assert claims.user_id == "user-1"
    assert claims.email == "a@b.com"


def test_client_id_used_as_default_audience(keys):
    priv, pub = keys
    p = _provider(pub, client_id="the-client")  # no explicit audience
    token = _sign(priv, {"sub": "u2", "aud": "the-client", "iss": _ISSUER})
    assert p.decode_jwt(token).user_id == "u2"


def test_wrong_audience_rejected(keys):
    priv, pub = keys
    p = _provider(pub, audience="my-api")
    token = _sign(priv, {"sub": "u3", "aud": "someone-else", "iss": _ISSUER})
    with pytest.raises(HTTPException) as exc:
        p.decode_jwt(token)
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected(keys):
    priv, pub = keys
    p = _provider(pub, audience="my-api")
    token = _sign(priv, {"sub": "u4", "aud": "my-api", "iss": "https://evil.example.com"})
    with pytest.raises(HTTPException) as exc:
        p.decode_jwt(token)
    assert exc.value.status_code == 401


def test_no_audience_configured_skips_aud_check(keys):
    priv, pub = keys
    p = _provider(pub)  # no client_id, no audience → verify_aud False
    token = _sign(priv, {"sub": "u5", "iss": _ISSUER})  # token carries no aud
    assert p.decode_jwt(token).user_id == "u5"


def test_tampered_signature_rejected(keys):
    priv, pub = keys
    p = _provider(pub, audience="my-api")
    token = _sign(priv, {"sub": "u6", "aud": "my-api", "iss": _ISSUER})
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(HTTPException) as exc:
        p.decode_jwt(tampered)
    assert exc.value.status_code == 401


def test_delete_user_is_noop(keys):
    _, pub = keys
    # Must not raise — best-effort no-op for a generic OIDC IdP.
    _provider(pub).delete_user("user-1")
