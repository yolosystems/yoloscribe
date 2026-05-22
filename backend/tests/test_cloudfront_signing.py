"""Unit tests for CloudFront signed cookie generation (YOL-127)."""

import base64
import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

import cloudfront_signing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_test_key():
    """Generate a minimal RSA-2048 key for testing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(private_key) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _decode_cf_b64(s: str) -> bytes:
    """Reverse CloudFront-safe base64: -→+, _→=, ~→/"""
    return base64.b64decode(s.replace("-", "+").replace("_", "=").replace("~", "/"))


# ---------------------------------------------------------------------------
# _cf_b64
# ---------------------------------------------------------------------------

class TestCfB64:
    def test_roundtrip(self):
        data = b"hello world +/="
        encoded = cloudfront_signing._cf_b64(data)
        assert _decode_cf_b64(encoded) == data

    def test_no_plus(self):
        # Encode enough bytes that + would appear in standard base64.
        # b"\xfb\xef" encodes to "++" in standard base64.
        encoded = cloudfront_signing._cf_b64(b"\xfb\xef")
        assert "+" not in encoded

    def test_no_equals(self):
        encoded = cloudfront_signing._cf_b64(b"x")  # "eA==" in standard b64
        assert "=" not in encoded

    def test_no_slash(self):
        # b"\xff\xff" encodes to "//" in standard base64.
        encoded = cloudfront_signing._cf_b64(b"\xff\xff")
        assert "/" not in encoded


# ---------------------------------------------------------------------------
# load_signing_key
# ---------------------------------------------------------------------------

class TestLoadSigningKey:
    def setup_method(self):
        # Reset module state before each test.
        cloudfront_signing._private_key = None

    def teardown_method(self):
        cloudfront_signing._private_key = None

    def test_loads_valid_pem(self):
        private_key = _generate_test_key()
        pem = _key_pem(private_key)

        class _Store:
            def get(self, key):
                return pem

        cloudfront_signing.load_signing_key(_Store())
        assert cloudfront_signing.is_configured()

    def test_missing_secret_leaves_unconfigured(self):
        class _Store:
            def get(self, key):
                return None

        cloudfront_signing.load_signing_key(_Store())
        assert not cloudfront_signing.is_configured()

    def test_invalid_pem_leaves_unconfigured(self):
        class _Store:
            def get(self, key):
                return "not-a-pem"

        cloudfront_signing.load_signing_key(_Store())
        assert not cloudfront_signing.is_configured()


# ---------------------------------------------------------------------------
# sign_media_cookies
# ---------------------------------------------------------------------------

class TestSignMediaCookies:
    def setup_method(self):
        cloudfront_signing._private_key = None
        self._private_key = _generate_test_key()
        cloudfront_signing._private_key = self._private_key

    def teardown_method(self):
        cloudfront_signing._private_key = None

    def _sign(self, site="alice-site", domain="media.example.com", key_pair_id="KTEST123"):
        return cloudfront_signing.sign_media_cookies(
            cloudfront_domain=domain,
            site=site,
            key_pair_id=key_pair_id,
        )

    def test_returns_three_cookies(self):
        cookies = self._sign()
        assert set(cookies.keys()) == {
            "CloudFront-Policy",
            "CloudFront-Signature",
            "CloudFront-Key-Pair-Id",
        }

    def test_key_pair_id_matches(self):
        cookies = self._sign(key_pair_id="KTEST456")
        assert cookies["CloudFront-Key-Pair-Id"] == "KTEST456"

    def test_policy_contains_correct_resource(self):
        cookies = self._sign(site="my-site", domain="cf.example.com")
        policy_json = _decode_cf_b64(cookies["CloudFront-Policy"]).decode()
        policy = json.loads(policy_json)
        resource = policy["Statement"][0]["Resource"]
        assert resource == "https://cf.example.com/my-site/*"

    def test_policy_expiry_is_in_future(self):
        before = int(time.time())
        cookies = self._sign()
        after = int(time.time())
        policy_json = _decode_cf_b64(cookies["CloudFront-Policy"]).decode()
        policy = json.loads(policy_json)
        expiry = policy["Statement"][0]["Condition"]["DateLessThan"]["AWS:EpochTime"]
        assert expiry > before
        assert expiry <= after + cloudfront_signing.COOKIE_TTL + 2  # small tolerance

    def test_policy_expiry_is_approximately_one_hour(self):
        now = int(time.time())
        cookies = self._sign()
        policy_json = _decode_cf_b64(cookies["CloudFront-Policy"]).decode()
        policy = json.loads(policy_json)
        expiry = policy["Statement"][0]["Condition"]["DateLessThan"]["AWS:EpochTime"]
        assert abs(expiry - (now + 3600)) <= 5  # within 5-second tolerance

    def test_signature_verifies_against_policy(self):
        cookies = self._sign()
        policy_bytes = _decode_cf_b64(cookies["CloudFront-Policy"])
        signature_bytes = _decode_cf_b64(cookies["CloudFront-Signature"])

        # Verify using the corresponding public key.
        public_key = self._private_key.public_key()
        # Should not raise.
        public_key.verify(signature_bytes, policy_bytes, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303

    def test_different_sites_produce_different_policies(self):
        cookies_a = self._sign(site="alice")
        cookies_b = self._sign(site="bob")
        assert cookies_a["CloudFront-Policy"] != cookies_b["CloudFront-Policy"]
        assert cookies_a["CloudFront-Signature"] != cookies_b["CloudFront-Signature"]

    def test_raises_when_key_not_loaded(self):
        cloudfront_signing._private_key = None
        with pytest.raises(RuntimeError, match="not loaded"):
            self._sign()

    def test_policy_is_compact_json(self):
        cookies = self._sign()
        policy_json = _decode_cf_b64(cookies["CloudFront-Policy"]).decode()
        # Compact JSON has no spaces around separators.
        assert " " not in policy_json

    def test_cookie_values_have_no_standard_b64_chars(self):
        cookies = self._sign()
        for name in ("CloudFront-Policy", "CloudFront-Signature"):
            val = cookies[name]
            assert "+" not in val, f"{name} contains +"
            assert "=" not in val, f"{name} contains ="
            assert "/" not in val, f"{name} contains /"
