"""R7 — Memory ambience.

Contract §5.6 / §8: the agent MUST apply path-scoped Yolo Brain method to a
page structuring without being explicitly told to read memory — memory
surfaced as an MCP resource (e.g. `memory://current`, `page-index://current`).

No MCP *resource* endpoint exists anywhere in `backend/mcp_server.py` today —
confirmed by inspecting its full tool list: `read_memory`/`write_memory` are
callable *tools* (the agent must be told to call them), not ambient
*resources* that arrive in context automatically. This is exactly the "Future:
Ambient Memory Context" gap the Yolo Brain implementation plan already
flags as pending. Reported here as a structural NOT_IMPLEMENTED rather than a
synthetic pass, so it appears uniformly in the R1-R7 table.
"""
from __future__ import annotations

import pytest

from .support.report import REPORT, RResult


@pytest.mark.conformance
def test_r7_memory_ambience():
    REPORT.record(
        RResult(
            id="R7",
            name="Memory ambience",
            status="NOT_IMPLEMENTED",
            detail=(
                "No MCP resource endpoint (memory://current, page-index://current) "
                "exists in backend/mcp_server.py. read_memory/write_memory are "
                "callable tools only — an agent must be explicitly told to call them, "
                "which is exactly what R7 says MUST NOT be required. This is Yolo "
                "Brain's flagged 'Future: Ambient Memory Context' work, not started."
            ),
        )
    )
