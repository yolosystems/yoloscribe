"""Persistent token storage backed by a JSON file, keyed by server URL."""

import json
import os
import time
from pathlib import Path
from typing import Optional


class TokenStore:
    """
    Stores OAuth tokens per server URL in a JSON file.
    File permissions are restricted to owner-only (0o600).
    """

    def __init__(self, path: str = "~/.mcp_oauth_tokens.json") -> None:
        self.path = Path(path).expanduser()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, server_url: str, token_data: dict) -> None:
        """Persist token data for *server_url*.  Computes expires_at from expires_in."""
        all_tokens = self._load_all()
        entry = dict(token_data)
        entry["_stored_at"] = int(time.time())
        if "expires_in" in token_data and "expires_at" not in token_data:
            entry["expires_at"] = int(time.time()) + int(token_data["expires_in"])
        all_tokens[server_url] = entry
        self._write(all_tokens)
        print(f"Tokens saved to {self.path}")

    def delete(self, server_url: str) -> None:
        all_tokens = self._load_all()
        if server_url in all_tokens:
            del all_tokens[server_url]
            self._write(all_tokens)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, server_url: str) -> Optional[dict]:
        return self._load_all().get(server_url)

    def get_access_token(self, server_url: str) -> Optional[str]:
        """Return the access token if present and not expired (with 60 s buffer)."""
        token = self.load(server_url)
        if not token:
            return None
        if "expires_at" in token and token["expires_at"] < time.time() + 60:
            return None
        return token.get("access_token")

    def is_expired(self, server_url: str) -> bool:
        token = self.load(server_url)
        if not token:
            return True
        if "expires_at" not in token:
            return False
        return token["expires_at"] < time.time() + 60

    def has_refresh_token(self, server_url: str) -> bool:
        token = self.load(server_url)
        return bool(token and token.get("refresh_token"))

    def list_servers(self) -> list[str]:
        return list(self._load_all().keys())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_all(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(self.path, 0o600)
