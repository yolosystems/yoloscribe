"""OAuth 2.1 Proof Key for Code Exchange (PKCE) - RFC 7636."""

import base64
import hashlib
import secrets


class PKCEChallenge:
    """
    Generates a PKCE code verifier and S256 challenge pair.
    Required by OAuth 2.1 for all public clients.
    """

    def __init__(self) -> None:
        # High-entropy code verifier: 64 URL-safe base64 chars
        self.verifier: str = secrets.token_urlsafe(48)
        # S256 challenge = BASE64URL(SHA256(ASCII(verifier)))
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        self.challenge: str = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        self.challenge_method: str = "S256"
