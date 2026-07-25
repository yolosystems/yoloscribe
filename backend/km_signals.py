"""Mapping from YoloScribe mutation events to YoloBrain KM signals (YOL-492).

YoloScribe observes what a tool call *already did* and forwards the
appropriately-typed knowledge-management signal to any configured SignalSink.
Third-party callers (Claude Code, Cowork, agent-runner) never name a signal
type — the server infers it from the mutation. See
projects/yoloscribe/feature-backlog/native-yolobrain-signal-emission in the
wiki for the design rationale.

The param shapes here mirror ``yolobrain.signals.catalog`` (a separate package
YoloScribe cannot import); YoloBrain validates them on its side. ``page_type``
is the clustering key (YoloBrain derives session ``km-{page_type}-page`` from
it), so it must be a descriptive *category* token, not a per-page identifier.
YoloScribe has no explicit page-type taxonomy, so we derive it from the page
path's topic hierarchy — see ``derive_page_type``.

Each builder returns ``(signal_type, params)`` ready to hand to
``SignalSink.emit(site, signal_type, params)``. The five mutation-shaped types
covered here are ``page_structured`` (wiki_create), ``content_routed``
(wiki_update), ``agent_provisioned`` (agent_create*), and ``notification_sent``
(notify). ``page_enriched`` needs a source-skill/placement that a raw MCP call
doesn't carry, so it is intentionally out of scope for server-side emission.
"""

from __future__ import annotations

import re

# Markdown ATX headings (## Foo) become the KM `sections` list. Setext headings
# and code-fenced `#` lines are deliberately ignored — good enough for the
# structural fingerprint clustering keys on.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.*\S)[ \t]*$", re.MULTILINE)


def _singularize(word: str) -> str:
    """Naive plural→singular for path segments (accounts→account, notes→note).

    Words that are already singular or don't end in a simple plural 's'
    (feature-backlog, notes→note, class→class) are left effectively intact.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def derive_page_type(page_path: str) -> str:
    """Derive a KM ``page_type`` from a page path's topic hierarchy.

    The *immediate parent* segment names what kind of page the leaf is — the
    most descriptive category for clustering
    (``projects/yoloscribe/feature-backlog/native-signal`` → ``feature-backlog``).
    A top-level page has no parent, so its own slug is used
    (``notes`` → ``note``). The root page → ``page``. Leading-dot system
    segments (``.user``) are skipped so ``.user/ingest`` → ``ingest``.
    """
    segments = [s for s in page_path.strip("/").split("/") if s and not s.startswith(".")]
    if not segments:
        return "page"
    if len(segments) == 1:
        return _singularize(segments[0])
    return _singularize(segments[-2])


def parse_sections(markdown: str) -> list[str]:
    """Return the ATX heading texts of a markdown document, in order."""
    return _HEADING_RE.findall(markdown or "")


def _target(page_path: str) -> dict[str, str]:
    return {"system": "yoloscribe", "path": page_path}


def page_structured_signal(page_path: str, content: str) -> tuple[str, dict]:
    """wiki_create → a new page is given structure (its section skeleton)."""
    return "page_structured", {
        "page_type": derive_page_type(page_path),
        "format": "markdown",
        "sections": parse_sections(content),
        "target": _target(page_path),
    }


def content_routed_signal(page_path: str, integration: str = "replace") -> tuple[str, dict]:
    """wiki_update → content is filed into an existing page.

    ``wiki_update`` is a full-page replace (``WikiPageMarkdownFile.write``), so
    the integration mode is ``replace``.
    """
    return "content_routed", {
        "page_type": derive_page_type(page_path),
        "format": "markdown",
        "integration": integration,
        "target": _target(page_path),
    }


def agent_provisioned_signal(
    page_path: str, agent_type: str, skills: list[str], trigger: str
) -> tuple[str, dict]:
    """agent_create* → an agent is provisioned on a page."""
    return "agent_provisioned", {
        "page_type": derive_page_type(page_path),
        "agent_type": agent_type,
        "skills": list(skills or []),
        "trigger": trigger,
        "host": {"path": page_path},
    }


def proposal_accepted_signal(page_path: str, what: str = "content change") -> tuple[str, dict]:
    """accept-proposed → the owner accepted an agent's staged change.

    Emitted from the REST accept endpoint, not an MCP tool — the owner's UI
    action is a real code path, so the signal is inferable server-side.
    """
    return "proposal_accepted", {
        "what": what,
        "page_type": derive_page_type(page_path),
        "target": _target(page_path),
    }


def proposal_rejected_signal(
    page_path: str, what: str = "content change", correction: str = ""
) -> tuple[str, dict]:
    """reject-proposed → the owner discarded an agent's staged change.

    ``correction`` is the KM learning hook (what the owner would have wanted
    instead) but the discard endpoint captures no reason today, so it is
    emitted empty. Wiring a reject-reason through the UI is a product follow-up.
    """
    return "proposal_rejected", {
        "what": what,
        "correction": correction,
        "page_type": derive_page_type(page_path),
        "target": _target(page_path),
    }


def notification_sent_signal(event: str, page_path: str = "") -> tuple[str, dict]:
    """notify → a notification was written to the owner's inbox.

    ``page_type``/``target`` are only attached when the notification is about a
    concrete page; site-level events (no page_path) omit them, matching the
    optional fields on YoloBrain's ``NotificationSentParams``.
    """
    params: dict = {"channel": "notifications", "event": event}
    if page_path:
        params["page_type"] = derive_page_type(page_path)
        params["target"] = _target(page_path)
    return "notification_sent", params


def notification_suppressed_signal(
    event: str, reason: str, channel: str = "notifications", page_path: str = ""
) -> tuple[str, dict]:
    """A notification the agent *chose not to send* — a no-write decision signal.

    Recorded via a ``notification_suppressed`` no-dispatch notification-log
    entry (see NO_DISPATCH_EVENTS / YOL-494). ``reason`` is what makes it a
    learning signal — why the agent decided suppression was correct.
    """
    params: dict = {"channel": channel, "event": event, "reason": reason}
    if page_path:
        params["page_type"] = derive_page_type(page_path)
        params["target"] = _target(page_path)
    return "notification_suppressed", params


def user_instruction_signal(instruction: str, domain: str = "general") -> tuple[str, dict]:
    """A freeform communicative act — the user telling the agent how to behave.

    Has no mutation to observe, so it is emitted explicitly. ``domain`` buckets
    the instruction (e.g. "retrieve", "present", "structure") for clustering;
    it defaults to "general" and is universal-scoped on the YoloBrain side.
    """
    return "user_instruction", {"instruction": instruction, "domain": domain}
