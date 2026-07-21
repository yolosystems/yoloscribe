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
