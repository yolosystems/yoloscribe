"""Tests for AES-256-GCM token encryption/decryption."""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _patch_key(monkeypatch, key_bytes: bytes = os.urandom(32)) -> bytes:
    import discord_bot.crypto as crypto_mod

    monkeypatch.setattr(crypto_mod, "_key", lambda: key_bytes)
    return key_bytes


class TestEncryptDecrypt:
    def test_roundtrip(self, monkeypatch):
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload, decrypt_payload

        token, site = "as_" + "a" * 64, "my-site"
        encrypted = encrypt_payload(token, site)
        assert decrypt_payload(encrypted) == (token, site)

    def test_output_is_base64(self, monkeypatch):
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload

        encrypted = encrypt_payload("as_" + "b" * 64, "site")
        base64.b64decode(encrypted)  # raises if not valid b64

    def test_nonce_is_12_bytes(self, monkeypatch):
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload

        encrypted = encrypt_payload("as_" + "c" * 64, "site")
        raw = base64.b64decode(encrypted)
        # First 12 bytes = nonce; rest = ciphertext + 16-byte GCM tag
        assert len(raw) > 12

    def test_different_encryptions_differ(self, monkeypatch):
        """Each call uses a fresh random nonce so output is never deterministic."""
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload

        a = encrypt_payload("as_" + "d" * 64, "site")
        b = encrypt_payload("as_" + "d" * 64, "site")
        assert a != b

    def test_tampered_ciphertext_raises(self, monkeypatch):
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload, decrypt_payload

        encrypted = encrypt_payload("as_" + "e" * 64, "site")
        raw = bytearray(base64.b64decode(encrypted))
        raw[-1] ^= 0xFF  # Flip a byte in the GCM tag
        tampered = base64.b64encode(bytes(raw)).decode()

        with pytest.raises(Exception):
            decrypt_payload(tampered)

    def test_wrong_key_raises(self, monkeypatch):
        key_a = os.urandom(32)
        key_b = os.urandom(32)

        import discord_bot.crypto as crypto_mod

        monkeypatch.setattr(crypto_mod, "_key", lambda: key_a)
        from discord_bot.crypto import encrypt_payload

        encrypted = encrypt_payload("as_" + "f" * 64, "site")

        monkeypatch.setattr(crypto_mod, "_key", lambda: key_b)
        from discord_bot.crypto import decrypt_payload

        with pytest.raises(Exception):
            decrypt_payload(encrypted)

    def test_site_name_preserved(self, monkeypatch):
        _patch_key(monkeypatch)
        from discord_bot.crypto import encrypt_payload, decrypt_payload

        _, site = decrypt_payload(encrypt_payload("as_" + "g" * 64, "knuth-home"))
        assert site == "knuth-home"


class TestKeySize:
    def test_invalid_key_raises_on_encrypt(self, monkeypatch):
        import discord_bot.crypto as crypto_mod

        monkeypatch.setattr(crypto_mod, "_key", lambda: b"short")
        from discord_bot.crypto import encrypt_payload

        with pytest.raises(Exception):
            encrypt_payload("as_" + "h" * 64, "site")
