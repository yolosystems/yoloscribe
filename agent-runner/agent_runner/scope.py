"""Scope resolution for cross-page agents.

Determines whether a given page falls within an agent's declared scope,
enforcing the hierarchical permission ceiling (agents cannot exceed the
page subtree they are defined in).
"""

from __future__ import annotations

import fnmatch


def agent_home(agent_md_key: str) -> str:
    """Return the home directory prefix from an agent.md S3 key.

    The home is everything up to (not including) the .agents/ component.

    e.g. "knuth/projects/.agents/sync/agent.md" → "knuth/projects/"
         "knuth/.agents/summary/agent.md"       → "knuth/"
    """
    parts = agent_md_key.split("/")
    try:
        idx = next(i for i, p in enumerate(parts) if p == ".agents")
    except StopIteration:
        return ""
    return "/".join(parts[:idx]) + "/" if idx > 0 else ""


def _page_rel(home: str, content_key: str) -> str | None:
    """Return the path of the page relative to home, or None if out of tree.

    e.g. home="knuth/projects/", content_key="knuth/projects/blog/post-1/content.md"
         → "blog/post-1"
         home="knuth/projects/", content_key="knuth/content.md"
         → None  (above home — permission ceiling)
    """
    if not content_key.startswith(home):
        return None
    # Strip home prefix and trailing /content.md
    rel = content_key[len(home):]
    if rel == "content.md":
        return ""  # The home page itself
    if rel.endswith("/content.md"):
        return rel[: -len("/content.md")]
    return None  # Non-content key; not a page


def _matches_pattern(rel: str, pattern: str) -> bool:
    """Return True if rel matches the scope pattern.

    Patterns use standard glob conventions:
        *   matches a single path segment (does not cross /)
        **  matches zero or more path segments
        ?   matches a single character within a segment

    Patterns containing '..' are always rejected.
    """
    pattern = pattern.strip("/")
    pat_parts = pattern.split("/") if pattern else []
    path_parts = rel.split("/") if rel else []

    def _match(pp: list[str], rp: list[str]) -> bool:
        if not pp and not rp:
            return True
        if pp and pp[0] == "**":
            tail = pp[1:]
            # ** matches zero or more segments — try each position
            for i in range(len(rp) + 1):
                if _match(tail, rp[i:]):
                    return True
            return False
        if not pp or not rp:
            return False
        if fnmatch.fnmatch(rp[0], pp[0]):
            return _match(pp[1:], rp[1:])
        return False

    return _match(pat_parts, path_parts)


def is_in_scope(agent_md_key: str, scope: list[str], content_key: str) -> bool:
    """Return True if content_key falls within the agent's declared scope.

    Args:
        agent_md_key:  S3 key of the agent.md file.
        scope:         List of glob patterns relative to the agent's home.
                       Empty list → agent is limited to its own page.
        content_key:   S3 key of the target page's content.md.

    Patterns that contain '..' are silently ignored (permission ceiling
    enforcement; agents cannot reach above their home directory).
    """
    home = agent_home(agent_md_key)
    if not home:
        return False

    rel = _page_rel(home, content_key)
    if rel is None:
        return False  # Page is outside (or above) the agent's home tree

    if not scope:
        # No scope declared: agent applies to its own page only
        return rel == ""

    for pattern in scope:
        # Reject any attempt to escape the home directory
        if ".." in pattern.split("/"):
            continue
        if _matches_pattern(rel, pattern):
            return True

    return False
