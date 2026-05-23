import json
import logging
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from yoloscribe_io.secrets import (
    LocalSecretStore,
    SecretsManagerStore,
    SecretStore,
    SupabaseSecretStore,
    UserSecret,
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

class _ResourceNotFound(Exception):
    pass


class _MockSMClient:
    """Minimal boto3 Secrets Manager client double."""

    class exceptions:
        ResourceNotFoundException = _ResourceNotFound

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})

    def get_secret_value(self, SecretId: str) -> dict:
        if SecretId not in self._store:
            raise self.exceptions.ResourceNotFoundException()
        return {"SecretString": self._store[SecretId]}

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        if SecretId not in self._store:
            raise self.exceptions.ResourceNotFoundException()
        self._store[SecretId] = SecretString

    def create_secret(self, Name: str, SecretString: str, Description: str = "") -> None:
        self._store[Name] = SecretString

    def delete_secret(self, SecretId: str, ForceDeleteWithoutRecovery: bool = False) -> None:
        if SecretId not in self._store:
            raise self.exceptions.ResourceNotFoundException()
        del self._store[SecretId]


class _StubUserSecret(UserSecret):
    """Concrete UserSecret for testing — key is fixed at construction."""

    def __init__(self, user_id: str, store: SecretStore, key_suffix: str = "stub") -> None:
        super().__init__(user_id, store)
        self._suffix = key_suffix

    @property
    def _key(self) -> str:
        return f"yoloscribe/{self._user_id}/stub/{self._suffix}"


def _fake_urlopen(responses: dict):
    """Return a mock urlopen that dispatches on request URL."""
    def _urlopen(req):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        body = responses.get(url, b"[]")
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp
    return _urlopen


# ── LocalSecretStore ──────────────────────────────────────────────────────────

@pytest.fixture
def local() -> LocalSecretStore:
    return LocalSecretStore()


def test_local_get_missing_returns_none(local):
    assert local.get("no/such/key") is None


def test_local_get_returns_stored_value(local):
    local.put("k", "v")
    assert local.get("k") == "v"


def test_local_put_overwrites(local):
    local.put("k", "first")
    local.put("k", "second")
    assert local.get("k") == "second"


def test_local_delete_removes_key(local):
    local.put("k", "v")
    local.delete("k")
    assert local.get("k") is None


def test_local_delete_missing_is_noop(local):
    local.delete("never/existed")  # must not raise


def test_local_exists_true(local):
    local.put("k", "v")
    assert local.exists("k") is True


def test_local_exists_false(local):
    assert local.exists("k") is False


def test_local_initial_values():
    store = LocalSecretStore({"a": "1", "b": "2"})
    assert store.get("a") == "1"
    assert store.get("b") == "2"


# ── SecretsManagerStore ───────────────────────────────────────────────────────

@pytest.fixture
def sm() -> SecretsManagerStore:
    return SecretsManagerStore(sm_client=_MockSMClient())


@pytest.fixture
def sm_prefilled() -> SecretsManagerStore:
    return SecretsManagerStore(sm_client=_MockSMClient({"existing/key": "secret-value"}))


def test_sm_get_returns_secret(sm_prefilled):
    assert sm_prefilled.get("existing/key") == "secret-value"


def test_sm_get_missing_returns_none(sm):
    assert sm.get("no/such") is None


def test_sm_get_other_error_returns_none_and_warns(caplog):
    client = _MockSMClient()
    client.get_secret_value = MagicMock(side_effect=RuntimeError("network error"))
    store = SecretsManagerStore(sm_client=client)
    with caplog.at_level(logging.WARNING, logger="yoloscribe_io.secrets"):
        result = store.get("k")
    assert result is None
    assert "network error" in caplog.text


def test_sm_put_updates_existing(sm_prefilled):
    sm_prefilled.put("existing/key", "new-value")
    assert sm_prefilled.get("existing/key") == "new-value"


def test_sm_put_creates_new_secret(sm):
    sm.put("new/key", "value")
    assert sm.get("new/key") == "value"


def test_sm_delete_removes_secret(sm_prefilled):
    sm_prefilled.delete("existing/key")
    assert sm_prefilled.get("existing/key") is None


def test_sm_delete_missing_is_noop(sm):
    sm.delete("never/existed")  # must not raise


def test_sm_delete_other_error_logs_warning(caplog):
    client = _MockSMClient({"k": "v"})
    client.delete_secret = MagicMock(side_effect=RuntimeError("oops"))
    store = SecretsManagerStore(sm_client=client)
    with caplog.at_level(logging.WARNING, logger="yoloscribe_io.secrets"):
        store.delete("k")
    assert "oops" in caplog.text


def test_sm_exists_true(sm_prefilled):
    assert sm_prefilled.exists("existing/key") is True


def test_sm_exists_false(sm):
    assert sm.exists("k") is False


# ── SupabaseSecretStore ───────────────────────────────────────────────────────

BASE_URL = "https://abc.supabase.co"
API_KEY = "test-key"


@pytest.fixture
def supabase() -> SupabaseSecretStore:
    return SupabaseSecretStore(supabase_url=BASE_URL, supabase_key=API_KEY)


def test_supabase_get_returns_value(supabase):
    body = json.dumps([{"value": "secret123"}]).encode()
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_resp
        assert supabase.get("some/key") == "secret123"


def test_supabase_get_returns_none_when_empty(supabase):
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_resp
        assert supabase.get("missing/key") is None


def test_supabase_get_returns_none_on_error(supabase, caplog):
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        with caplog.at_level(logging.WARNING, logger="yoloscribe_io.secrets"):
            result = supabase.get("k")
    assert result is None
    assert "connection refused" in caplog.text


def test_supabase_put_posts_to_rest(supabase):
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = MagicMock()
        supabase.put("my/key", "my-value")
        assert mock_open.called
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        assert body == {"key": "my/key", "value": "my-value"}
        assert req.get_header("Prefer") == "resolution=merge-duplicates"


def test_supabase_delete_sends_delete_request(supabase):
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = MagicMock()
        supabase.delete("my/key")
        req = mock_open.call_args[0][0]
        assert req.get_method() == "DELETE"
        assert "eq.my%2Fkey" in req.full_url or "eq.my/key" in req.full_url


def test_supabase_delete_logs_on_error(supabase, caplog):
    with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
        with caplog.at_level(logging.WARNING, logger="yoloscribe_io.secrets"):
            supabase.delete("k")
    assert "timeout" in caplog.text


# ── UserSecret ────────────────────────────────────────────────────────────────

@pytest.fixture
def secret_store() -> LocalSecretStore:
    return LocalSecretStore()


def test_user_secret_user_id(secret_store):
    s = _StubUserSecret("user-42", secret_store)
    assert s.user_id == "user-42"


def test_user_secret_get_returns_none_when_missing(secret_store):
    s = _StubUserSecret("u1", secret_store)
    assert s.get() is None


def test_user_secret_put_stores_value(secret_store):
    s = _StubUserSecret("u1", secret_store)
    s.put("token-data")
    assert s.get() == "token-data"


def test_user_secret_delete_removes_value(secret_store):
    s = _StubUserSecret("u1", secret_store)
    s.put("v")
    s.delete()
    assert s.get() is None


def test_user_secret_delete_missing_is_noop(secret_store):
    s = _StubUserSecret("u1", secret_store)
    s.delete()  # must not raise


def test_user_secret_exists_true(secret_store):
    s = _StubUserSecret("u1", secret_store)
    s.put("v")
    assert s.exists() is True


def test_user_secret_exists_false(secret_store):
    s = _StubUserSecret("u1", secret_store)
    assert s.exists() is False


def test_user_secret_key_encapsulated_in_subclass(secret_store):
    s = _StubUserSecret("u1", secret_store, key_suffix="oauth")
    s.put("token")
    # Application code sees only s.get() — raw key never needed
    assert secret_store.get("yoloscribe/u1/stub/oauth") == "token"


def test_user_secret_different_users_are_isolated(secret_store):
    s1 = _StubUserSecret("alice", secret_store)
    s2 = _StubUserSecret("bob", secret_store)
    s1.put("alice-token")
    assert s2.get() is None


def test_user_secret_different_suffixes_are_isolated(secret_store):
    s1 = _StubUserSecret("u1", secret_store, key_suffix="linear")
    s2 = _StubUserSecret("u1", secret_store, key_suffix="github")
    s1.put("linear-tok")
    assert s2.get() is None
