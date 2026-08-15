"""Ingest provenance: where a document came from, and what happened to it (YOL-552).

A provenance record has two lifecycle stages, and the split is the point:

* **Staged** — written when a file is queued into ``.user/ingest/``, before any
  agent has looked at it. Carries what only the uploader knows: the `intent`
  behind the ingest and the `source_url` the file came from.
* **Landed** — written when the ingest agent routes the document to a page.
  Carries the staged fields plus the routing outcome, the extractor that
  produced the text, and the retention choice for the original bytes.

The landed record is stored as the page's media-asset sidecar
(``{site}/{page}/.media/{filename}.json``) rather than in a store of its own.
That file already exists per-file and per-page, already has CDN serving,
listing, and deletion behind it, and already emits lifecycle events. Provenance
is a set of extra fields on it, not a parallel structure.

**Why this must be captured at ingest and cannot be added later:** the source of
a document is unrecoverable once the upload is over. A document ingested without
it can never be retroactively gated (YOL-553), re-verified, or selectively
re-extracted. The design page calls for one provenance primitive serving three
consumers — learning attribution, source ACL inheritance, and extraction
fidelity — and this is it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .storage import StorageBackend

log = logging.getLogger(__name__)

# Staged records live in a sibling prefix rather than beside the bytes, so the
# ingest queue listing stays a list of documents. `ingest_list_pending` filters
# this prefix explicitly.
INGEST_PREFIX = ".user/ingest/"
STAGED_PREFIX = ".user/ingest/.provenance/"


class Retention:
    """What happens to the original bytes after the document is routed."""

    DELETE = "delete"          # bytes discarded; the record survives as a pointer
    YOLOSCRIBE = "yoloscribe"  # kept as a page asset
    EXTERNAL = "external"      # copied to a system of record — needs tool OAuth, not yet built

    ALL = (DELETE, YOLOSCRIBE, EXTERNAL)
    DEFAULT = YOLOSCRIBE


class SourceStatus:
    """Whether `source_url` has been checked against the ingested bytes.

    `source_url` is an assertion by whoever ingested the document, and nothing
    stops a caller — or a prompt-injected 3P assistant — naming a public URL
    while uploading a restricted file. Unverified it is a fine human pointer and
    an unsound access-control anchor, which is exactly what YOL-553 wants to use
    it for. Only ``VERIFIED`` may gate anything.
    """

    NONE = "none"            # no source_url was given
    UNVERIFIED = "unverified"  # claimed but not checked
    VERIFIED = "verified"      # fetched at ingest and fingerprint-matched
    MISMATCH = "mismatch"      # fetched and did NOT match — treat as hostile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Provenance:
    """Where a document came from and what became of it.

    Every field is optional with an inert default, so a record can be written at
    upload time knowing almost nothing and enriched as the document moves.
    """

    # ── staged: known at upload ───────────────────────────────────────────────
    filename: str = ""
    intent: str = ""            # why this document is being ingested (YOL-527 discipline)
    source_url: str = ""        # where it came from; rendered as a link when it parses as one
    source_status: str = SourceStatus.NONE
    ingested_at: str = field(default_factory=_now)
    ingested_by: str = ""       # user_id

    # ── landed: known after routing ───────────────────────────────────────────
    page_path: str = ""         # where the summary was filed
    extractor: str = ""         # what produced the text
    extractor_version: str = ""
    retention: str = Retention.DEFAULT
    routed_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        """Build from stored JSON, ignoring unknown keys.

        Tolerant by design: these records outlive the code that wrote them, and a
        field added in a later version must not make an older record unreadable.
        """
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def land(
        self,
        *,
        page_path: str,
        extractor: str = "",
        extractor_version: str = "",
        retention: str = "",
    ) -> "Provenance":
        """Return the landed form of this record, preserving the staged fields."""
        if retention and retention not in Retention.ALL:
            raise ValueError(
                f"Unknown retention {retention!r}. Expected one of {Retention.ALL}."
            )
        return Provenance(
            filename=self.filename,
            intent=self.intent,
            source_url=self.source_url,
            source_status=self.source_status,
            ingested_at=self.ingested_at,
            ingested_by=self.ingested_by,
            page_path=page_path,
            extractor=extractor,
            extractor_version=extractor_version,
            retention=retention or self.retention,
            routed_at=_now(),
        )

    @property
    def gates_access(self) -> bool:
        """Whether this record may act as an access-control anchor (YOL-553).

        Only a verified source qualifies. Everything else — no source, an
        unchecked claim, or a claim that failed its check — falls back to the
        rules for content YoloScribe cannot vouch for.
        """
        return self.source_status == SourceStatus.VERIFIED and bool(self.source_url)


# ── staged record I/O ─────────────────────────────────────────────────────────


def _staged_key(site: str, filename: str) -> str:
    return f"{site}/{STAGED_PREFIX}{filename}.json"


def write_staged(site: str, prov: Provenance, storage: StorageBackend) -> str:
    """Persist a staged provenance record for a queued ingest file."""
    key = _staged_key(site, prov.filename)
    storage.write(key, json.dumps(prov.to_dict(), indent=2), content_type="application/json")
    return key


def read_staged(site: str, filename: str, storage: StorageBackend) -> Provenance | None:
    """Load the staged record for a queued file, or None when absent.

    Absent is normal, not an error: files uploaded before this existed, and
    files dropped into the queue by other paths, simply have no record.
    """
    raw = storage.read(_staged_key(site, filename))
    if raw is None:
        return None
    try:
        return Provenance.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("provenance: malformed staged record for %s/%s: %s", site, filename, exc)
        return None


def delete_staged(site: str, filename: str, storage: StorageBackend) -> None:
    """Remove the staged record. Safe to call when none exists."""
    try:
        storage.delete(_staged_key(site, filename))
    except Exception:  # noqa: BLE001 — deleting a non-existent record is not a failure
        log.debug("provenance: no staged record to delete for %s/%s", site, filename)
