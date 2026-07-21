"""R6 — Capability failure/substitution.

Contract §5.5 / §8: a required capability absent from the tool proxy MUST
either trigger the substitution flow (candidates surfaced, choice emitted as
a signal) or fail loudly as `agent_failure` with a diagnostic — never a
silent partial run.

No capability/proxy resolution concept exists anywhere in this codebase today
— confirmed by inspecting the full `backend/mcp_server.py` tool list (no
capability/proxy-shaped tool exists) and agent-runner (which resolves skills
via `.skills/{name}/mcp.json` + bucket-root `.tools/{name}` directly, with no
substitution or failure-diagnostic layer). This is Phase 3 (Dissolve skills)
work per the re-architecture plan — reported here as a structural
NOT_IMPLEMENTED rather than a synthetic pass, so it appears uniformly in the
R1-R7 table.
"""
from __future__ import annotations

import pytest

from .support.report import REPORT, RResult


@pytest.mark.conformance
def test_r6_capability_failure():
    REPORT.record(
        RResult(
            id="R6",
            name="Capability failure/substitution",
            status="NOT_IMPLEMENTED",
            detail=(
                "No capability/proxy resolution exists in agent-runner or backend "
                "(mcp_server.py has no capability/proxy-shaped tool). Skills resolve "
                "via .skills/{name}/mcp.json + bucket-root .tools/{name} with no "
                "substitution or diagnostic-failure layer. This is Phase 3 (Dissolve "
                "skills) work, not yet started."
            ),
        )
    )
