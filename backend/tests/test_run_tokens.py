"""Unit tests for run-token signing and verification (RS256, backend-only key)."""

import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt
import pytest

import run_tokens


class _Store:
    """In-memory SecretsStore double."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data = dict(initial or {})
        self.put_calls: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str, description: str = "") -> None:
        self._data[key] = value
        self.put_calls.append((key, value))

    def exists(self, key: str) -> bool:
        return key in self._data

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


def _reset():
    run_tokens._private_key_pem = None
    run_tokens._public_key_pem = None
    run_tokens._kid = ""


class TestLoadSigningKey:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_loads_existing_key(self):
        store = _Store()
        store.put(run_tokens._SM_SECRET_NAME, json.dumps({
            "kid": "test-1", "algorithm": "RS256",
            "private_key_pem": "dummy", "public_key_pem": "dummy",
        }))
        run_tokens.load_signing_key(store, local_mode=False)
        assert run_tokens.is_configured()
        assert run_tokens.current_kid() == "test-1"

    def test_missing_key_non_local_leaves_unconfigured(self):
        store = _Store()
        run_tokens.load_signing_key(store, local_mode=False)
        assert not run_tokens.is_configured()
        assert store.put_calls == []

    def test_missing_key_local_mode_auto_generates_and_persists(self):
        store = _Store()
        run_tokens.load_signing_key(store, local_mode=True)
        assert run_tokens.is_configured()
        assert len(store.put_calls) == 1
        assert store.exists(run_tokens._SM_SECRET_NAME)

    def test_local_mode_reuses_existing_key_without_regenerating(self):
        store = _Store()
        store.put(run_tokens._SM_SECRET_NAME, json.dumps({
            "kid": "existing", "algorithm": "RS256",
            "private_key_pem": "dummy", "public_key_pem": "dummy",
        }))
        store.put_calls.clear()  # drop the seed call above; only care about load_signing_key's own writes
        run_tokens.load_signing_key(store, local_mode=True)
        assert run_tokens.current_kid() == "existing"
        assert store.put_calls == []  # never regenerated

    def test_corrupt_secret_leaves_unconfigured(self):
        store = _Store()
        store.put(run_tokens._SM_SECRET_NAME, "not-json")
        run_tokens.load_signing_key(store, local_mode=False)
        assert not run_tokens.is_configured()


class TestMintAndDecode:
    def setup_method(self):
        _reset()
        run_tokens.load_signing_key(_Store(), local_mode=True)

    def teardown_method(self):
        _reset()

    def test_round_trip_page(self):
        token = run_tokens.mint_run_token(
            site="alice-site", user_id="user-1", agent_name="tidy-bot",
            agent_type="page", page_path="features/auth",
        )
        claims = run_tokens.decode_run_token(token)
        assert claims.site == "alice-site"
        assert claims.user_id == "user-1"
        assert claims.agent_name == "tidy-bot"
        assert claims.agent_type == "page"
        assert claims.path_scope == [
            run_tokens.PathScopeEntry("features/auth", ["read", "write-content"])
        ]
        assert claims.run_id  # non-empty, unique per mint

    def test_round_trip_ingest_is_whole_tree(self):
        token = run_tokens.mint_run_token(
            site="alice-site", user_id="user-1", agent_name="ingester", agent_type="ingest",
        )
        claims = run_tokens.decode_run_token(token)
        assert claims.path_scope == [run_tokens.PathScopeEntry("", ["read", "write-content"])]

    def test_round_trip_notification_has_no_wiki_writes(self):
        token = run_tokens.mint_run_token(
            site="alice-site", user_id="user-1", agent_name="notifier", agent_type="notification",
        )
        claims = run_tokens.decode_run_token(token)
        assert claims.path_scope == [run_tokens.PathScopeEntry("", ["read", "notify"])]
        assert "write-content" not in claims.path_scope[0].operations

    def test_unknown_agent_type_rejected(self):
        with pytest.raises(ValueError, match="Unknown agent_type"):
            run_tokens.mint_run_token(
                site="s", user_id="u", agent_name="a", agent_type="bogus",
            )

    def test_two_mints_get_different_run_ids(self):
        t1 = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        t2 = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        assert run_tokens.decode_run_token(t1).run_id != run_tokens.decode_run_token(t2).run_id

    def test_expired_token_rejected(self):
        token = run_tokens.mint_run_token(
            site="s", user_id="u", agent_name="a", agent_type="page", ttl_seconds=-10,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            run_tokens.decode_run_token(token)

    def test_tampered_signature_rejected(self):
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        header, payload, signature = token.split(".")
        tampered = f"{header}.{payload}.{signature[:-4]}zzzz"
        with pytest.raises(jwt.InvalidSignatureError):
            run_tokens.decode_run_token(tampered)

    def test_tampered_payload_rejected(self):
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        header, payload, signature = token.split(".")
        # Flip a character in the payload segment.
        tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
        tampered = f"{header}.{tampered_payload}.{signature}"
        with pytest.raises(jwt.exceptions.DecodeError):
            run_tokens.decode_run_token(tampered)

    def test_wrong_key_cannot_verify(self):
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        # Swap in a different keypair post-mint — simulates a token signed
        # under a since-rotated key being presented to a verifier that only
        # trusts the current one.
        _reset()
        run_tokens.load_signing_key(_Store(), local_mode=True)
        with pytest.raises(jwt.InvalidSignatureError):
            run_tokens.decode_run_token(token)

    def test_default_ttl_is_15_minutes(self):
        before = int(time.time())
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        claims = run_tokens.decode_run_token(token)
        assert abs(claims.exp - claims.iat - run_tokens.DEFAULT_TTL_SECONDS) <= 1
        assert claims.iat >= before

    def test_mint_without_loaded_key_raises(self):
        _reset()
        with pytest.raises(RuntimeError, match="not loaded"):
            run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")

    def test_decode_without_loaded_key_raises(self):
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page")
        _reset()
        with pytest.raises(RuntimeError, match="not loaded"):
            run_tokens.decode_run_token(token)
