"""Librarian memory substrate — per-user preference memory for YoloScribe."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .markdown_file import _parse_frontmatter
from .storage import StorageBackend

# ── Certainty model ───────────────────────────────────────────────────────────

# Valid level strings and their certainty order (lower = more certain).
_LEVEL_ORDER: dict[str, int] = {
    "explicit": 0,
    "deductive": 1,
    "inductive": 2,
    "abductive": 3,
}

_VALID_LEVELS = frozenset(_LEVEL_ORDER)
_VALID_DOMAINS = frozenset({"ingest", "enrich", "retrieve", "notify", "present"})
_VALID_STATUSES = frozenset({"active", "decaying", "retired", "corrected"})

_CONCLUSIONS_SECTION_RE = re.compile(
    r"## Conclusions\n\n(.*?)(?=\n## |\Z)", re.DOTALL
)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class EvidenceEntry:
    type: str
    ref: str = ""
    at: str = ""
    note: str = ""


@dataclass
class Conclusion:
    id: str
    level: str           # one of _VALID_LEVELS
    domain: str          # one of _VALID_DOMAINS
    statement: str
    evidence: list[EvidenceEntry] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    confidence_trend: str = ""
    last_reinforced: str = ""
    status: str = "active"


@dataclass
class SignalEntry:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = ""


# ── Scaffolding rule enforcement ──────────────────────────────────────────────

def scaffolding_rule_violations(
    conclusion: Conclusion, by_id: dict[str, Conclusion]
) -> list[str]:
    """Return violation messages; empty list means the conclusion is valid.

    Rule: derived_from may only reference conclusions of equal or higher certainty
    (equal or lower _LEVEL_ORDER value).
    """
    violations: list[str] = []
    if conclusion.level not in _VALID_LEVELS:
        violations.append(f"unknown level '{conclusion.level}'")
        return violations
    this_order = _LEVEL_ORDER[conclusion.level]
    for ref_id in conclusion.derived_from:
        if ref_id not in by_id:
            violations.append(f"derived_from references unknown id '{ref_id}'")
            continue
        ref_level = by_id[ref_id].level
        ref_order = _LEVEL_ORDER.get(ref_level, 99)
        if ref_order > this_order:
            violations.append(
                f"derived_from '{ref_id}' (level={ref_level}) "
                f"is less certain than this conclusion (level={conclusion.level})"
            )
    return violations


# ── MemoryFile ────────────────────────────────────────────────────────────────

class MemoryFile:
    """Reads and writes .user/librarian/memory.md for a site."""

    _RELATIVE_PATH = ".user/librarian/memory.md"

    def __init__(self, site: str, storage: StorageBackend) -> None:
        self._site = site
        self._storage = storage
        self._key = f"{site}/{self._RELATIVE_PATH}"

    @property
    def key(self) -> str:
        return self._key

    def read(self) -> tuple[dict[str, Any], list[Conclusion]]:
        """Return (frontmatter, conclusions). Both empty when the file doesn't exist."""
        raw = self._storage.read(self._key) or ""
        return _parse_memory(raw)

    def write(self, frontmatter: dict[str, Any], conclusions: list[Conclusion]) -> None:
        self._storage.write(self._key, _build_memory_md(frontmatter, conclusions))

    def upsert(self, new_conclusions: list[Conclusion]) -> tuple[int, int, list[str]]:
        """Merge new_conclusions into existing memory, enforcing the scaffolding rule.

        Returns (created, updated, rejected_messages).
        Rejected conclusions are logged but never written.
        """
        fm, existing = self.read()
        by_id: dict[str, Conclusion] = {c.id: c for c in existing}
        created = updated = 0
        rejected: list[str] = []

        for c in new_conclusions:
            # Validate against the merged pool (existing + already-accepted new).
            check_pool = {**by_id, c.id: c}
            violations = scaffolding_rule_violations(c, check_pool)
            if violations:
                rejected.append(f"'{c.id}': {'; '.join(violations)}")
                continue
            if c.id in by_id:
                updated += 1
            else:
                created += 1
            by_id[c.id] = c

        fm.setdefault("schema_version", 1)
        fm["last_consolidated"] = _now_iso()
        self.write(fm, list(by_id.values()))
        return created, updated, rejected


# ── ArchetypeFile ─────────────────────────────────────────────────────────────

class ArchetypeFile:
    """Reads and writes .user/librarian/archetypes.md for a site.

    The archetypes file is an agent-maintained markdown document listing
    canonical agent templates. The Librarian reads it before creating agents
    to prevent duplicates, and writes to it after provisioning a new archetype.
    The file is human-editable.
    """

    _RELATIVE_PATH = ".user/librarian/archetypes.md"

    def __init__(self, site: str, storage: StorageBackend) -> None:
        self._site = site
        self._storage = storage
        self._key = f"{site}/{self._RELATIVE_PATH}"

    @property
    def key(self) -> str:
        return self._key

    def read(self) -> str:
        """Return the raw markdown content, or empty string if the file doesn't exist."""
        return self._storage.read(self._key) or ""

    def write(self, content: str) -> None:
        self._storage.write(self._key, content)


# ── SignalLog ─────────────────────────────────────────────────────────────────

class SignalLog:
    """Append-only preference signal log at .user/librarian/signal-log.md.

    New entries are prepended so the most recent signal is at the top.
    """

    _RELATIVE_PATH = ".user/librarian/signal-log.md"

    def __init__(self, site: str, storage: StorageBackend) -> None:
        self._site = site
        self._storage = storage
        self._key = f"{site}/{self._RELATIVE_PATH}"

    @property
    def key(self) -> str:
        return self._key

    def append(self, entry: SignalEntry) -> None:
        at = entry.at or _now_iso()
        try:
            ts = datetime.fromisoformat(at.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except ValueError:
            ts = at
        lines = [f"## {ts} — {entry.type}", ""]
        for k, v in entry.payload.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        formatted = "\n".join(lines) + "\n"
        existing = self._storage.read(self._key) or ""
        self._storage.write(self._key, formatted + existing)

    def read(self, limit: int = 50) -> str:
        """Return up to `limit` most-recent entries as raw markdown text."""
        raw = self._storage.read(self._key) or ""
        if not limit:
            return raw
        sections: list[str] = []
        current: list[str] = []
        for line in raw.splitlines(keepends=True):
            if line.startswith("## ") and current:
                sections.append("".join(current))
                if len(sections) >= limit:
                    current = []
                    break
                current = [line]
            else:
                current.append(line)
        if current and len(sections) < limit:
            sections.append("".join(current))
        return "".join(sections)

    def read_all(self) -> str:
        """Return the complete signal log without a limit."""
        return self._storage.read(self._key) or ""

    def rotate(self, hot_window_days: int, archive_key: str) -> int:
        """Move entries older than hot_window_days to archive_key.

        Returns the number of entries archived.
        Entries are kept in the hot log newest-first; old entries move to an
        archive segment at archive_key (also newest-first within that segment).
        """
        from datetime import timedelta

        raw = self._storage.read(self._key) or ""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=hot_window_days)

        hot_sections: list[str] = []
        archive_sections: list[str] = []
        current: list[str] = []
        current_ts: datetime | None = None

        def _flush() -> None:
            if not current:
                return
            block = "".join(current)
            if current_ts is not None and current_ts < cutoff:
                archive_sections.append(block)
            else:
                hot_sections.append(block)

        for line in raw.splitlines(keepends=True):
            if line.startswith("## "):
                _flush()
                current = [line]
                # Try to parse the timestamp from "## YYYY-MM-DD HH:MM UTC — type"
                m = re.match(r"## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", line)
                if m:
                    try:
                        current_ts = datetime.strptime(
                            m.group(1), "%Y-%m-%d %H:%M"
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        current_ts = None
                else:
                    current_ts = None
            else:
                current.append(line)
        _flush()

        if not archive_sections:
            return 0

        # Write updated hot log.
        self._storage.write(self._key, "".join(hot_sections))

        # Prepend archived sections to the existing archive segment.
        existing_archive = self._storage.read(archive_key) or ""
        self._storage.write(archive_key, "".join(archive_sections) + existing_archive)

        return len(archive_sections)


# ── Parsing / serialization ───────────────────────────────────────────────────

def _parse_memory(raw: str) -> tuple[dict[str, Any], list[Conclusion]]:
    import yaml

    fm, body = _parse_frontmatter(raw)
    m = _CONCLUSIONS_SECTION_RE.search(body)
    if not m:
        return fm, []
    raw_yaml = m.group(1).strip()
    if not raw_yaml:
        return fm, []
    try:
        items = yaml.safe_load(raw_yaml)
        if not isinstance(items, list):
            return fm, []
        return fm, [
            _conclusion_from_dict(item)
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]
    except Exception:
        return fm, []


def _conclusion_from_dict(d: dict[str, Any]) -> Conclusion:
    evidence = [
        EvidenceEntry(
            type=str(e.get("type", "")),
            ref=str(e.get("ref", "")),
            at=str(e.get("at", "")),
            note=str(e.get("note", "")),
        )
        for e in (d.get("evidence") or [])
        if isinstance(e, dict)
    ]
    return Conclusion(
        id=str(d.get("id", "")),
        level=str(d.get("level", "explicit")),
        domain=str(d.get("domain", "enrich")),
        statement=str(d.get("statement", "")),
        evidence=evidence,
        derived_from=[str(x) for x in (d.get("derived_from") or [])],
        confidence_trend=str(d.get("confidence_trend", "")),
        last_reinforced=str(d.get("last_reinforced", "")),
        status=str(d.get("status", "active")),
    )


def conclusion_to_dict(c: Conclusion) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": c.id,
        "level": c.level,
        "domain": c.domain,
        "statement": c.statement,
    }
    if c.evidence:
        d["evidence"] = [
            {k: v for k, v in {
                "type": e.type, "ref": e.ref, "at": e.at, "note": e.note
            }.items() if v}
            for e in c.evidence
        ]
    if c.derived_from:
        d["derived_from"] = c.derived_from
    if c.confidence_trend:
        d["confidence_trend"] = c.confidence_trend
    if c.last_reinforced:
        d["last_reinforced"] = c.last_reinforced
    d["status"] = c.status
    return d


def _build_memory_md(frontmatter: dict[str, Any], conclusions: list[Conclusion]) -> str:
    import yaml

    fm_str = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True
    ).strip()
    conclusions_yaml = (
        yaml.dump(
            [conclusion_to_dict(c) for c in conclusions],
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        if conclusions
        else ""
    )
    return (
        f"---\n{fm_str}\n---\n\n"
        f"# Librarian Memory\n\n"
        f"## Conclusions\n\n"
        f"{conclusions_yaml}"
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
