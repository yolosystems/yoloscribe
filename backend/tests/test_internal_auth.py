"""Unit tests for internal_auth.check_caller — the mint-endpoint's swappable auth check."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

import internal_auth
import config


class TestCheckCaller:
    def setup_method(self):
        self._orig = internal_auth.INTERNAL_MINT_SECRET

    def teardown_method(self):
        internal_auth.INTERNAL_MINT_SECRET = self._orig

    def test_correct_secret_passes(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "INTERNAL_MINT_SECRET", "sekrit")
        internal_auth.check_caller("sekrit")  # must not raise

    def test_wrong_secret_rejected(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "INTERNAL_MINT_SECRET", "sekrit")
        with pytest.raises(HTTPException) as exc_info:
            internal_auth.check_caller("wrong")
        assert exc_info.value.status_code == 403

    def test_empty_header_rejected(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "INTERNAL_MINT_SECRET", "sekrit")
        with pytest.raises(HTTPException):
            internal_auth.check_caller("")

    def test_unconfigured_secret_always_rejects(self, monkeypatch):
        # Even a matching empty string must not pass when the secret itself is unset —
        # otherwise an unconfigured deployment would silently accept ANY caller.
        monkeypatch.setattr(internal_auth, "INTERNAL_MINT_SECRET", "")
        with pytest.raises(HTTPException):
            internal_auth.check_caller("")


class TestCheckMessagingBot:
    """The messaging bot's credential is deliberately separate from the mint secret."""

    def test_correct_secret_passes(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        internal_auth.check_messaging_bot("bot-sekrit")  # must not raise

    def test_wrong_secret_rejected(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")
        with pytest.raises(HTTPException) as exc_info:
            internal_auth.check_messaging_bot("wrong")
        assert exc_info.value.status_code == 403

    def test_unconfigured_secret_always_rejects(self, monkeypatch):
        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "")
        with pytest.raises(HTTPException):
            internal_auth.check_messaging_bot("")

    def test_mint_secret_does_not_authorize_the_bot(self, monkeypatch):
        """The whole point of the split (YOL-523).

        The bot processes untrusted chat input; /internal/runs/mint accepts an
        arbitrary site + user_id. If one secret opened both doors, a compromised
        bot could mint run tokens for any site.
        """
        monkeypatch.setattr(internal_auth, "INTERNAL_MINT_SECRET", "mint-sekrit")
        monkeypatch.setattr(internal_auth, "MESSAGING_BOT_SECRET", "bot-sekrit")

        with pytest.raises(HTTPException):
            internal_auth.check_messaging_bot("mint-sekrit")
        with pytest.raises(HTTPException):
            internal_auth.check_caller("bot-sekrit")
