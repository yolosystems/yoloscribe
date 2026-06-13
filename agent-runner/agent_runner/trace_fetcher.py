"""Trace fetcher for eval annotation logs.

After a successful agent run with eval_log: true, polls the Phoenix REST API
for spans associated with the run's session ID, formats an annotation log
markdown file, and writes it to S3.

Environment variables:
    PHOENIX_API_ENDPOINT  Base URL for the Phoenix REST API (e.g. http://phoenix:6006).
                          When absent, the annotation log is written without trace data.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# Maximum total sleep across all retries (seconds).
_MAX_TOTAL_SLEEP_S = 30.0


def _phoenix_base_url() -> str:
    import os
    return os.environ.get("PHOENIX_API_ENDPOINT", "").rstrip("/")


def _fetch_spans(session_id: str, max_retries: int = 5) -> list[dict]:
    """Poll Phoenix REST API for spans by session ID with exponential backoff.

    Returns list of span dicts from Phoenix, or empty list if unavailable.
    """
    base_url = _phoenix_base_url()
    if not base_url:
        return []

    # Phoenix REST API: GET /v1/spans?filter[session_id]=<id>&project_name=default
    # The filter parameter uses the Phoenix span attribute filter syntax.
    encoded_id = urllib.parse.quote(session_id)
    url = (
        f"{base_url}/v1/spans"
        f"?project_name=default"
        f"&filter%5Bsession_id%5D={encoded_id}"
    )

    sleep_s = 2.0
    total_sleep = 0.0

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                spans = data.get("data", [])
                if spans:
                    log.info(
                        "Fetched %d spans for session %s on attempt %d",
                        len(spans), session_id[:8], attempt + 1,
                    )
                    return spans
                log.info(
                    "Phoenix returned 0 spans for session %s (attempt %d/%d)",
                    session_id[:8], attempt + 1, max_retries,
                )
        except Exception as exc:
            log.warning(
                "Phoenix API query failed (attempt %d/%d): %s",
                attempt + 1, max_retries, exc,
            )

        if attempt < max_retries - 1 and total_sleep < _MAX_TOTAL_SLEEP_S:
            actual_sleep = min(sleep_s, _MAX_TOTAL_SLEEP_S - total_sleep)
            log.info("Retrying in %.1fs...", actual_sleep)
            time.sleep(actual_sleep)
            total_sleep += actual_sleep
            sleep_s = min(sleep_s * 2, _MAX_TOTAL_SLEEP_S - total_sleep)

    log.warning("No spans found for session %s after %d attempts", session_id[:8], max_retries)
    return []


def _format_span_tree(spans: list[dict]) -> str:
    """Format Phoenix spans as a readable conversation section."""
    if not spans:
        return (
            "*Trace data not available. "
            "Set PHOENIX_API_ENDPOINT to enable full conversation traces.*\n"
        )

    lines: list[str] = []

    # Sort by start time if available; root spans (no parent) come first.
    def _start_time(s: dict) -> float:
        attrs = s.get("attributes", {})
        return float(attrs.get("start_time", 0))

    root_spans = [s for s in spans if not s.get("parent_id")]
    child_spans = [s for s in spans if s.get("parent_id")]

    root_spans.sort(key=_start_time)
    child_spans.sort(key=_start_time)

    turn = 0
    for span in root_spans:
        attrs = span.get("attributes", {})
        span_kind = attrs.get("openinference.span.kind", "")
        name = span.get("name", "")

        if span_kind in ("LLM", "CHAIN") or "llm" in name.lower():
            turn += 1
            lines.append(f"### Turn {turn}")
            lines.append("")

            input_val = attrs.get("input.value", "")
            output_val = attrs.get("output.value", "")

            if input_val:
                lines.append("**Input:**")
                lines.append(input_val[:2000])
                lines.append("")

            # Collect tool calls from child spans of this root span
            span_id = span.get("span_id", "")
            tool_calls = [
                c for c in child_spans
                if c.get("parent_id") == span_id
                and c.get("attributes", {}).get("openinference.span.kind") == "TOOL"
            ]
            if tool_calls:
                lines.append("**Tool calls:**")
                for tc in tool_calls:
                    tc_attrs = tc.get("attributes", {})
                    tc_name = tc.get("name", "unknown")
                    tc_input = tc_attrs.get("input.value", "")
                    tc_output = tc_attrs.get("output.value", "")
                    summary = (tc_output[:200] + "…") if len(tc_output) > 200 else tc_output
                    lines.append(f"- `{tc_name}({tc_input[:100]})` → {summary}")
                lines.append("")

            if output_val:
                lines.append("**Output:**")
                lines.append(output_val[:2000])
                lines.append("")

            lines.append("---")
            lines.append("")

    # Fallback: just show span names if we couldn't parse the tree
    if turn == 0:
        lines.append(f"*{len(spans)} span(s) recorded. Span tree could not be parsed.*")
        for s in spans[:10]:
            lines.append(f"- `{s.get('name', '?')}` ({s.get('attributes', {}).get('openinference.span.kind', '?')})")
        lines.append("")

    return "\n".join(lines)


def _run_log_filename(session_id: str) -> str:
    """Return the filename for this session's run log: YYYY-MM-DD-{8hex}.md."""
    date_str = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    short_id = session_id.replace("-", "")[:8]
    return f"{date_str}-{short_id}.md"


def write_eval_annotation_log(
    *,
    session_id: str,
    agent_name: str,
    page_path: str,
    agent_md_key: str,
    run_at: str,
    status: str,
    duration_s: float,
    tokens_used: int,
    model_key: str,
    storage,
) -> str | None:
    """Fetch traces from Phoenix and write an annotation log file to S3.

    Returns the S3 key of the written log, or None on failure.
    """
    spans = _fetch_spans(session_id)
    conversation_section = _format_span_tree(spans)

    # Derive S3 key: {site}/{page_path}/.agents/{agent_name}/runs/{filename}
    # agent_md_key is: {site}/{page_path}/.agents/{agent_name}/agent.md
    # We want:         {site}/{page_path}/.agents/{agent_name}/runs/{filename}
    runs_dir = agent_md_key[: -len("/agent.md")] + "/runs"
    filename = _run_log_filename(session_id)
    run_log_key = f"{runs_dir}/{filename}"

    content = _build_annotation_log(
        session_id=session_id,
        agent_name=agent_name,
        page_path=page_path,
        run_at=run_at,
        status=status,
        duration_s=duration_s,
        tokens_used=tokens_used,
        model_key=model_key,
        conversation_section=conversation_section,
    )

    try:
        storage.write(run_log_key, content)
        log.info("Wrote eval annotation log to %s", run_log_key)
        return run_log_key
    except Exception as exc:
        log.warning("Failed to write eval annotation log to %s: %s", run_log_key, exc)
        return None


def _build_annotation_log(
    *,
    session_id: str,
    agent_name: str,
    page_path: str,
    run_at: str,
    status: str,
    duration_s: float,
    tokens_used: int,
    model_key: str,
    conversation_section: str,
) -> str:
    short_id = session_id.replace("-", "")[:8]
    run_date = run_at[:10]

    frontmatter = "\n".join([
        "---",
        f"session_id: {session_id}",
        f"agent: {agent_name}",
        f"page: {page_path or '/'}",
        f"run_at: {run_at}",
        "---",
    ])

    status_icon = "✓" if status == "success" else "✗"
    tokens_str = f"{tokens_used:,}" if tokens_used else "—"

    return f"""{frontmatter}

# Run Log: {agent_name} — {run_date} ({short_id})

## Summary

| | |
|---|---|
| **Status** | {status_icon} {status} |
| **Duration** | {duration_s:.1f}s |
| **Model** | {model_key} |
| **Tokens used** | {tokens_str} |
| **Session ID** | `{session_id}` |

## Conversation

{conversation_section}
## Annotations

<!-- Fill in the fields below and save to submit annotations to Phoenix. Leave all fields blank to skip. -->

**Rating:** <!-- Excellent / Good / Poor / Bad -->

**Notes:** <!-- What worked well or didn't? Free text. -->

**Correction:** <!-- What should the agent have done differently? Optional. -->
"""
