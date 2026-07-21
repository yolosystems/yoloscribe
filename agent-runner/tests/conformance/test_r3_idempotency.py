"""R3 — Idempotency.

Contract §5.3 / §8: redelivering the same ingest trigger twice routes each
file once (R3a); a scheduled agent MUST NOT overlap itself for a given
(site, page, agent) — single-flight via lock/lease (R3b).

Reported as two separate lines, not one — they test genuinely independent
things and, per a fresh codebase survey, currently diverge: the DDB
conditional-put lock primitive itself is correct (R3b's first check), but
`polling_worker.py`'s `LOCAL_RUNNER=true` dispatch path (`_run_local`) never
calls it at all — only `_process_message_k8s` (the real K8s dispatch path)
does. That's a real gap independent of the broader re-architecture, worth
surfacing on its own rather than folding into a single pass/fail.
"""
from __future__ import annotations

import inspect

import pytest
from agent_runner import polling_worker
from agent_runner.agents.ingest import IngestAgent
from yoloscribe_io import AgentDefinition
from yoloscribe_io.storage import LocalStorageBackend

from .support.lock_table import acquire_page_lock, dynamodb_client
from .support.report import REPORT, RResult
from .support.scratch_site import new_site
from .support.scripted_model import ScriptedModel, ToolCall

_INGEST_SCRIPT = [
    [ToolCall("ingest_list_pending", {})],
    [ToolCall("ingest_read", {"filename": "note.md"})],
    [ToolCall("wiki_list_pages", {})],
    [ToolCall("wiki_write", {"page_path": "notes", "content": "# Notes\n\nRouted content.\n"})],
    [ToolCall("ingest_mark_processed", {"filename": "note.md"})],
    [ToolCall("ingest_complete", {"summary": "Routed note.md to notes."})],
    "Ingest run complete.",
]

_REDELIVERY_SCRIPT = [
    [ToolCall("ingest_list_pending", {})],
    [ToolCall("ingest_complete", {"summary": "Nothing to process."})],
    "Nothing to do.",
]


@pytest.mark.conformance
def test_r3a_ingest_redelivery_idempotency():
    storage = LocalStorageBackend()
    site = "conformance-r3a"
    notifications: list[tuple] = []

    def notify_fn(event_type: str, payload: dict, user_id: str = "") -> None:
        notifications.append((event_type, payload, user_id))

    storage.write(f"{site}/.user/ingest/note.md", "Meeting notes about the roadmap.")
    agent_def = AgentDefinition(name="ingester", trigger="schedule", type="ingest")

    first = IngestAgent(
        agent_def=agent_def, site=site, page_path=".user/ingest", storage=storage,
        mcp_tools=[], model=ScriptedModel(list(_INGEST_SCRIPT)),
        user_id="test-user", notify_fn=notify_fn,
    )
    first.run("Process the ingest queue.")
    content_after_first = storage.read(f"{site}/notes/content.md")

    # Simulate redelivery of the same trigger: a fresh agent instance (as a
    # redelivered SQS message would construct), same storage/site.
    second = IngestAgent(
        agent_def=agent_def, site=site, page_path=".user/ingest", storage=storage,
        mcp_tools=[], model=ScriptedModel(list(_REDELIVERY_SCRIPT)),
        user_id="test-user", notify_fn=notify_fn,
    )
    second.run("Process the ingest queue.")
    content_after_second = storage.read(f"{site}/notes/content.md")
    pending_after_second = second.ingest_list_pending()

    checklist = {
        "page content not duplicated by redelivery": content_after_first == content_after_second,
        "pending list empty after redelivery (file already moved to processed/)": (
            pending_after_second == "No pending files."
        ),
    }
    passed = all(checklist.values())
    REPORT.record(
        RResult(
            id="R3a",
            name="Ingest redelivery idempotency",
            status="PASS" if passed else "FAIL",
            detail=(
                "content-state idempotency holds: ingest_mark_processed moves the file out "
                "of the pending prefix, so a redelivered trigger finds nothing to route."
                if passed
                else "redelivery produced a different outcome than the first run — see checklist."
            ),
            checklist=checklist,
        )
    )
    assert passed, checklist


@pytest.mark.conformance_live
def test_r3b_single_flight_lock():
    ddb = dynamodb_client()
    user_id = "conformance-r3b-user"
    content_key = f"{new_site()}/content.md"

    first_acquire = acquire_page_lock(ddb, user_id, content_key)
    second_acquire = acquire_page_lock(ddb, user_id, content_key)

    lock_primitive_correct = first_acquire is True and second_acquire is False

    # Structural check: does the LOCAL_RUNNER dispatch path (`_run_local`)
    # actually call the lock primitive? Verified via source inspection rather
    # than a live SQS dispatch run — standing up a full LOCAL_RUNNER message
    # flow is out of scope for S0.1's baseline.
    run_local_source = inspect.getsource(polling_worker._run_local)
    dispatch_calls_lock = "_acquire_page_lock" in run_local_source

    checklist = {
        "lock primitive: first acquire succeeds": first_acquire is True,
        "lock primitive: concurrent second acquire is rejected": second_acquire is False,
        "lock enforced on the LOCAL_RUNNER dispatch path (_run_local calls _acquire_page_lock)": (
            dispatch_calls_lock
        ),
    }

    if lock_primitive_correct and dispatch_calls_lock:
        status = "PASS"
    elif lock_primitive_correct and not dispatch_calls_lock:
        status = "PARTIAL"
    else:
        status = "FAIL"

    detail = (
        "lock primitive (_acquire_page_lock against real dynamodb-local): "
        f"{'correct' if lock_primitive_correct else 'INCORRECT'}. "
        "dispatch-path enforcement: "
        + (
            "_run_local calls it."
            if dispatch_calls_lock
            else (
                "_run_local (the LOCAL_RUNNER dispatch path) never calls "
                "_acquire_page_lock — only _process_message_k8s (the real K8s "
                "dispatch path) does. Local dev / this harness's own dispatch "
                "path currently has zero single-flight protection."
            )
        )
    )

    REPORT.record(RResult(id="R3b", name="Single-flight lock", status=status, detail=detail, checklist=checklist))
    assert lock_primitive_correct, checklist
