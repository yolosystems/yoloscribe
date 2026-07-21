"""Run-token signing and verification — RS256, backend-only private key.

Run tokens authenticate a single agent-runner job invocation against the MCP
server, scoped to exactly what that agent type is permitted to touch (see
PathScopeEntry). The private key never leaves this process; the public key
may be handed to any verifier (agent-runner today; a future third-party
runtime per the Phase 4 OAuth registration/token-exchange design — see
projects/yoloscribe/ideas/delegation-token in the wiki).

Loaded once at backend startup via load_signing_key() (called from config.py),
mirroring cloudfront_signing.py's lifecycle. Unlike CloudFront signing (which
is skipped entirely in LOCAL_MODE since there's no local CloudFront), the
run-token key is required for MCP auth to work at all, so LOCAL_MODE
auto-generates and persists a dev keypair on first boot when one isn't found.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

log = logging.getLogger(__name__)

_SM_SECRET_NAME = "yoloscribe/run-token-signing-key"
_ISSUER = "yoloscribe-backend"
_AUDIENCE = "yoloscribe-mcp"
_ALGORITHM = "RS256"

DEFAULT_TTL_SECONDS = 900  # 15 min — generous single-run ceiling; P1.4 adds heartbeat refresh + tightens this.

_VALID_AGENT_TYPES = frozenset({"page", "ingest", "notification"})

# Populated at startup by load_signing_key().
_private_key_pem: str | None = None
_public_key_pem: str | None = None
_kid: str = ""


@dataclasses.dataclass
class PathScopeEntry:
    path_prefix: str
    operations: list[str]

    def to_dict(self) -> dict:
        return {"path_prefix": self.path_prefix, "operations": self.operations}

    @classmethod
    def from_dict(cls, d: dict) -> PathScopeEntry:
        return cls(path_prefix=str(d["path_prefix"]), operations=[str(op) for op in d["operations"]])


@dataclasses.dataclass
class RunTokenClaims:
    run_id: str
    site: str
    agent_name: str
    agent_type: str
    path_scope: list[PathScopeEntry]
    user_id: str
    exp: int
    iat: int


def _default_path_scope(agent_type: str, page_path: str) -> list[PathScopeEntry]:
    """Per-agent-type containment floor. See the Delegation Token wiki doc §3."""
    if agent_type == "page":
        return [PathScopeEntry(page_path, ["read", "write-content"])]
    if agent_type == "ingest":
        # Whole tree — ingest routes to dynamic destinations. Never write-settings/write-agent/delete.
        return [PathScopeEntry("", ["read", "write-content"])]
    if agent_type == "notification":
        # No wiki writes at all.
        return [PathScopeEntry("", ["read", "notify"])]
    raise ValueError(f"Unknown agent_type '{agent_type}'; must be one of {sorted(_VALID_AGENT_TYPES)}")


def load_signing_key(secrets_store, *, local_mode: bool = False) -> None:
    """Load the RS256 run-token signing key from the secrets store.

    In LOCAL_MODE, auto-generates and persists a dev keypair on first boot if
    none exists yet — local dev shouldn't require a manual provisioning step.
    In production, a missing key logs a warning and leaves signing/verification
    unavailable (mint requests fail loud; run-token auth simply never matches)
    rather than crashing the whole process, matching cloudfront_signing.py's
    degrade-gracefully precedent.
    """
    global _private_key_pem, _public_key_pem, _kid
    raw = secrets_store.get(_SM_SECRET_NAME)
    if raw is None:
        if not local_mode:
            log.warning(
                "Run-token signing key not found in secrets store (%s). "
                "Run-token minting and verification will be unavailable until provisioned.",
                _SM_SECRET_NAME,
            )
            return
        log.info("No run-token signing key found in LOCAL_MODE — generating a dev keypair.")
        raw = _generate_and_store(secrets_store)
    try:
        data = json.loads(raw)
        _private_key_pem = data["private_key_pem"]
        _public_key_pem = data["public_key_pem"]
        _kid = data["kid"]
        log.info("Run-token signing key loaded (kid=%s)", _kid)
    except Exception as exc:
        log.error("Failed to load run-token signing key: %s", exc)


def _generate_and_store(secrets_store) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    raw = json.dumps({
        "kid": f"dev-{uuid.uuid4().hex[:8]}",
        "algorithm": _ALGORITHM,
        "private_key_pem": private_pem,
        "public_key_pem": public_pem,
    })
    secrets_store.put(_SM_SECRET_NAME, raw, description="Run-token RS256 signing key (auto-generated, LOCAL_MODE)")
    return raw


def is_configured() -> bool:
    return _private_key_pem is not None


def current_kid() -> str:
    """The active signing key's kid — used by callers to peek at a bearer token's
    header and decide whether it's a run token before attempting to decode it."""
    return _kid


def mint_run_token(
    *,
    site: str,
    user_id: str,
    agent_name: str,
    agent_type: str,
    page_path: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    if _private_key_pem is None:
        raise RuntimeError("Run-token signing key is not loaded")
    path_scope = _default_path_scope(agent_type, page_path)

    now = int(time.time())
    payload = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": user_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "run_id": str(uuid.uuid4()),
        "site": site,
        "agent_name": agent_name,
        "agent_type": agent_type,
        "path_scope": [entry.to_dict() for entry in path_scope],
    }
    return jwt.encode(payload, _private_key_pem, algorithm=_ALGORITHM, headers={"kid": _kid})


def decode_run_token(token: str) -> RunTokenClaims:
    if _public_key_pem is None:
        raise RuntimeError("Run-token signing key is not loaded")
    payload = jwt.decode(token, _public_key_pem, algorithms=[_ALGORITHM], issuer=_ISSUER, audience=_AUDIENCE)
    return RunTokenClaims(
        run_id=payload["run_id"],
        site=payload["site"],
        agent_name=payload["agent_name"],
        agent_type=payload["agent_type"],
        path_scope=[PathScopeEntry.from_dict(d) for d in payload["path_scope"]],
        user_id=payload["sub"],
        exp=payload["exp"],
        iat=payload["iat"],
    )
