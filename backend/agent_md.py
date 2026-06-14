"""Shim: re-exports from yoloscribe_io.

All agent.md parsing and building now lives in the yoloscribe_io library.
This module exists only for backwards compatibility with any remaining
internal imports that have not yet been migrated.
"""

from yoloscribe_io import (  # noqa: F401
    AgentDefinition,
    AgentDefinitionError,
    build_agent_md,
    parse_agent_md,
)
from yoloscribe_io.agent_page import AGENT_NAME_RE  # noqa: F401
from yoloscribe_io.markdown_file import _parse_frontmatter  # noqa: F401
