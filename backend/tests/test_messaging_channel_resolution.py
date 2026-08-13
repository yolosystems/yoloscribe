"""Unit tests for channel → owner resolution (YOL-523).

The messaging bot names a channel and authenticates as itself; the backend
resolves that channel to (user_id, site) through the API-token binding. These
tests pin the behavior that matters for security: a channel resolves only while
its underlying token is live, and no credential is ever stored or returned.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException


class _FakeMessagingRepo:
    def __init__(self, binding=None):
        self._binding = binding
        self.upserted = []

    def get_by_channel(self, platform, channel_id):
        return self._binding

    def upsert(self, platform, api_token_id, connection):
        self.upserted.append((platform, api_token_id, connection))
        return "cfg-1"

    def list_by_token_ids(self, token_ids):
        return []

    def get(self, config_id):
        return None

    def delete(self, config_id):
        return None


class _FakeTokenRepo:
    def __init__(self, by_id=None, by_hash=None):
        self._by_id = by_id
        self._by_hash = by_hash

    def get_by_id(self, token_id):
        return self._by_id

    def get_by_hash(self, token_hash):
        return self._by_hash

    def insert_token(self, *a, **k):
        return "tok"

    def list_tokens(self, user_id):
        return []

    def revoke_token(self, token_id, user_id):
        return True

    def update_last_used(self, token_id):
        return None


def _live_token():
    return {"id": "tok-1", "user_id": "user-1", "site_name": "acme", "expires_at": None}


def _install(monkeypatch, messaging_repo, token_repo):
    import config as cfg
    monkeypatch.setattr(cfg, "messaging_config_repo", messaging_repo, raising=False)
    monkeypatch.setattr(cfg, "api_token_repo", token_repo, raising=False)


class TestResolveChannel:
    def test_linked_channel_resolves_to_owner(self, monkeypatch):
        from routers.internal import _resolve_channel
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=_live_token()))
        assert _resolve_channel("discord", "chan-1") == ("user-1", "acme")

    def test_unlinked_channel_is_404(self, monkeypatch):
        from routers.internal import _resolve_channel
        _install(monkeypatch, _FakeMessagingRepo(None), _FakeTokenRepo())
        with pytest.raises(HTTPException) as exc:
            _resolve_channel("discord", "chan-1")
        assert exc.value.status_code == 404

    def test_revoked_token_disconnects_the_channel(self, monkeypatch):
        """get_by_id returns None for revoked tokens, so revocation cascades."""
        from routers.internal import _resolve_channel
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=None))
        with pytest.raises(HTTPException) as exc:
            _resolve_channel("discord", "chan-1")
        assert exc.value.status_code == 404

    def test_expired_token_disconnects_the_channel(self, monkeypatch):
        from routers.internal import _resolve_channel
        expired = {**_live_token(), "expires_at": "2020-01-01T00:00:00Z"}
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=expired))
        with pytest.raises(HTTPException) as exc:
            _resolve_channel("discord", "chan-1")
        assert exc.value.status_code == 404

    def test_far_future_expiry_still_resolves(self, monkeypatch):
        from routers.internal import _resolve_channel
        live = {**_live_token(), "expires_at": "2999-01-01T00:00:00Z"}
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=live))
        assert _resolve_channel("discord", "chan-1") == ("user-1", "acme")

    def test_unparseable_expiry_treated_as_non_expiring(self, monkeypatch):
        """Matches auth.py's resolve_api_token, so the two paths agree."""
        from routers.internal import _resolve_channel
        odd = {**_live_token(), "expires_at": "not-a-date"}
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=odd))
        assert _resolve_channel("discord", "chan-1") == ("user-1", "acme")

    def test_token_without_site_does_not_resolve(self, monkeypatch):
        from routers.internal import _resolve_channel
        siteless = {**_live_token(), "site_name": ""}
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=siteless))
        with pytest.raises(HTTPException):
            _resolve_channel("discord", "chan-1")

    def test_unconfigured_storage_is_404_not_500(self, monkeypatch):
        from routers.internal import _resolve_channel
        _install(monkeypatch, None, None)
        with pytest.raises(HTTPException) as exc:
            _resolve_channel("discord", "chan-1")
        assert exc.value.status_code == 404


class TestLinkStoresNoCredential:
    def test_link_persists_only_the_token_id(self, monkeypatch):
        """The pasted token must not reach storage — only its row ID (YOL-523)."""
        import internal_auth
        from routers.internal import LinkChannelRequest, link_channel

        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        repo = _FakeMessagingRepo()
        _install(monkeypatch, repo, _FakeTokenRepo(by_hash=_live_token()))

        resp = asyncio.run(link_channel(
            LinkChannelRequest(
                platform="discord",
                channel_id="chan-1",
                api_token="as_" + "a" * 64,
                connection={"guild_id": "g1"},
            ),
            x_internal_auth="bot-sekrit",
        ))

        assert resp.site_name == "acme"
        assert len(repo.upserted) == 1
        platform, api_token_id, connection = repo.upserted[0]
        assert platform == "discord"
        assert api_token_id == "tok-1"
        assert connection == {"guild_id": "g1", "channel_id": "chan-1"}
        # No field anywhere in the persisted payload may contain the raw token.
        assert "as_" not in str(connection)

    def test_link_rejects_unknown_token(self, monkeypatch):
        import internal_auth
        from routers.internal import LinkChannelRequest, link_channel

        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        repo = _FakeMessagingRepo()
        _install(monkeypatch, repo, _FakeTokenRepo(by_hash=None))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(link_channel(
                LinkChannelRequest(platform="discord", channel_id="c", api_token="as_bad"),
                x_internal_auth="bot-sekrit",
            ))
        assert exc.value.status_code == 401
        assert repo.upserted == []

    def test_link_requires_the_bot_secret(self, monkeypatch):
        import internal_auth
        from routers.internal import LinkChannelRequest, link_channel

        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        repo = _FakeMessagingRepo()
        _install(monkeypatch, repo, _FakeTokenRepo(by_hash=_live_token()))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(link_channel(
                LinkChannelRequest(platform="discord", channel_id="c", api_token="as_x"),
                x_internal_auth="wrong",
            ))
        assert exc.value.status_code == 403
        assert repo.upserted == []


class TestBindingLeaksNoCredential:
    def test_binding_returns_only_linked_and_site(self, monkeypatch):
        import internal_auth
        from routers.internal import get_binding

        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        _install(monkeypatch,
                 _FakeMessagingRepo({"id": "cfg-1", "api_token_id": "tok-1"}),
                 _FakeTokenRepo(by_id=_live_token()))

        resp = asyncio.run(get_binding("discord", "chan-1", x_internal_auth="bot-sekrit"))
        assert resp == {"linked": True, "site_name": "acme"}

    def test_unlinked_binding_reports_false(self, monkeypatch):
        import internal_auth
        from routers.internal import get_binding

        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        _install(monkeypatch, _FakeMessagingRepo(None), _FakeTokenRepo())

        resp = asyncio.run(get_binding("discord", "chan-1", x_internal_auth="bot-sekrit"))
        assert resp == {"linked": False}
