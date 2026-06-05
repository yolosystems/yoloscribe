"""Markdown-aware chunker for YoloScribe page content.

Inspired by qmd (https://github.com/tobi/qmd). Splits on structural markdown
boundaries (headings, code fences) with a target chunk size of ~900 tokens
(approximated as 3600 chars at 4 chars/token) and 15% overlap.

Also extracts YAML frontmatter `tags:` for use in FTS5 and vector metadata.
"""

from __future__ import annotations

import re

_TARGET_CHARS = 3600
_OVERLAP_CHARS = int(_TARGET_CHARS * 0.15)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter_tags(content: str) -> tuple[list[str], str]:
    """Return (tags, body) where body has the frontmatter stripped."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return [], content

    fm = m.group(1)
    body = content[m.end():]

    # tags: [foo, bar]
    inline = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.MULTILINE)
    if inline:
        tags = [t.strip().strip("\"'") for t in inline.group(1).split(",") if t.strip()]
        return tags, body

    # tags:\n  - foo\n  - bar
    block = re.search(r"^tags:\s*\n((?:[ \t]*-[ \t]+.+\n?)*)", fm, re.MULTILINE)
    if block:
        tags = [re.sub(r"^[ \t]*-[ \t]+", "", line).strip()
                for line in block.group(1).splitlines() if line.strip()]
        return tags, body

    return [], body


def _split_into_sections(text: str) -> list[str]:
    """Split text into structural sections at heading and code-fence boundaries."""
    split_points = [0]
    in_fence = False

    for m in re.finditer(r"^(#{1,6} |```)", text, re.MULTILINE):
        token = m.group()
        if token.startswith("```"):
            in_fence = not in_fence
            if in_fence:
                split_points.append(m.start())
        elif not in_fence:
            split_points.append(m.start())

    split_points.append(len(text))
    split_points = sorted(set(split_points))

    sections = []
    for i in range(len(split_points) - 1):
        s = text[split_points[i]:split_points[i + 1]].strip()
        if s:
            sections.append(s)
    return sections


def _split_oversized(text: str) -> list[str]:
    """Split an oversized section on double-newline paragraph boundaries."""
    chunks: list[str] = []
    parts: list[str] = []
    length = 0
    for para in re.split(r"\n\n+", text):
        if length + len(para) > _TARGET_CHARS and parts:
            chunks.append("\n\n".join(parts))
            parts, length = [], 0
        parts.append(para)
        length += len(para)
    if parts:
        chunks.append("\n\n".join(parts))
    return chunks


def chunk_markdown(content: str) -> list[dict]:
    """Chunk markdown content into overlapping segments.

    Returns list of {"text": str, "tags": list[str]}.
    """
    tags, body = parse_frontmatter_tags(content)
    sections = _split_into_sections(body)
    if not sections:
        return []

    raw_chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for section in sections:
        sec_len = len(section)
        if current_len + sec_len > _TARGET_CHARS and current_parts:
            raw_chunks.append("\n\n".join(current_parts))
            # Overlap: keep trailing parts that fit within the overlap budget
            overlap: list[str] = []
            budget = 0
            for part in reversed(current_parts):
                if budget + len(part) <= _OVERLAP_CHARS:
                    overlap.insert(0, part)
                    budget += len(part)
                else:
                    break
            current_parts = overlap
            current_len = budget
        current_parts.append(section)
        current_len += sec_len

    if current_parts:
        raw_chunks.append("\n\n".join(current_parts))

    # Break any chunk that ended up much larger than the target
    final: list[str] = []
    for chunk in raw_chunks:
        if len(chunk) > _TARGET_CHARS * 2:
            final.extend(_split_oversized(chunk))
        else:
            final.append(chunk)

    return [{"text": c, "tags": tags} for c in final if c.strip()]
