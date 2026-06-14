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


def _otlp_auth_headers() -> dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS into a dict for use in Phoenix REST calls.

    Format: comma-separated key=value pairs, e.g. "api_key=abc123,other=val".
    """
    import os
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    headers: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers


def _fetch_spans(session_id: str, max_retries: int = 5) -> list[dict]:
    """Poll Phoenix REST API for spans by session ID with exponential backoff.

    Every span in the trace carries session.id (stamped by _SessionStamper in
    agent_runner.py), so a single attribute-filtered query is sufficient.

    Returns list of span dicts from Phoenix, or empty list if unavailable.
    """
    base_url = _phoenix_base_url()
    if not base_url:
        return []

    # attribute filter splits on first ':' only — UUID session IDs are safe.
    encoded_attr = urllib.parse.quote(f"session.id:{session_id}")
    url = f"{base_url}/v1/projects/default/spans?attribute={encoded_attr}&limit=1000"

    auth_headers = {"Accept": "application/json", **_otlp_auth_headers()}

    sleep_s = 5.0
    total_sleep = 0.0

    def _query() -> list[dict]:
        req = urllib.request.Request(url, headers=auth_headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("data", [])

    for attempt in range(max_retries):
        try:
            spans = _query()
            if spans:
                log.info(
                    "Fetched %d spans for session %s on attempt %d — waiting 5s for stragglers",
                    len(spans), session_id[:8], attempt + 1,
                )
                time.sleep(5.0)
                try:
                    spans = _query()
                    log.info(
                        "Final fetch: %d spans for session %s",
                        len(spans), session_id[:8],
                    )
                except Exception as exc:
                    log.warning("Final span fetch failed for session %s: %s", session_id[:8], exc)
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


def _parse_event_content(raw) -> str:
    """Parse a gen_ai event content field (JSON array of content blocks) to plain text."""
    if not raw:
        return ""
    try:
        blocks = json.loads(raw) if isinstance(raw, str) else raw
        parts = []
        for block in blocks:
            if "text" in block:
                parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                inp = json.dumps(tu.get("input", {}))
                parts.append(f"`{tu.get('name', '?')}({inp[:300]})`")
            elif "toolResult" in block:
                tr = block["toolResult"]
                result_parts = [c.get("text", "") for c in tr.get("content", [])]
                parts.append(" ".join(result_parts)[:400])
        return "\n".join(parts)
    except Exception:
        return str(raw)[:500]


def _format_span_tree(spans: list[dict]) -> str:
    """Format Phoenix spans as: system prompt → tool calls → final output."""
    if not spans:
        return (
            "*Trace data not available. "
            "Set PHOENIX_API_ENDPOINT to enable full conversation traces.*\n"
        )

    lines: list[str] = []

    # ── System prompt — from first LLM span ──────────────────────────────────
    llm_spans = sorted(
        [s for s in spans if s.get("span_kind") == "LLM"],
        key=lambda s: s.get("start_time", ""),
    )
    for span in llm_spans:
        for ev in span.get("events", []):
            if ev.get("name") == "gen_ai.system.message":
                text = _parse_event_content(ev.get("attributes", {}).get("content", ""))
                if text:
                    lines.append("**System:**")
                    lines.append(text[:800])
                    lines.append("")
                break
        else:
            continue
        break

    # ── Tool calls — from TOOL spans, chronological ───────────────────────────
    tool_spans = sorted(
        [s for s in spans if s.get("span_kind") == "TOOL"],
        key=lambda s: s.get("start_time", ""),
    )
    for ts in tool_spans:
        attrs = ts.get("attributes", {})
        tool_name = attrs.get("gen_ai.tool.name") or ts.get("name", "unknown tool")
        tool_input = ""
        tool_result = ""
        for ev in ts.get("events", []):
            ev_name = ev.get("name", "")
            ev_attrs = ev.get("attributes", {})
            if ev_name == "gen_ai.tool.message":
                raw = ev_attrs.get("content", "")
                if raw:
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                        tool_input = json.dumps(parsed, ensure_ascii=False)[:400]
                    except Exception:
                        tool_input = str(raw)[:400]
            elif ev_name == "gen_ai.choice":
                msg = ev_attrs.get("message", "")
                tool_result = _parse_event_content(msg)[:400] if msg else ""

        lines.append(f"**Tool call:** `{tool_name}`")
        if tool_input:
            lines.append(f"Input: `{tool_input}`")
        if tool_result:
            lines.append(f"Result: {tool_result}")
        lines.append("")

    # ── Final output — prefer AGENT span, fall back to last end_turn LLM span ─
    final_output = ""

    agent_spans = [s for s in spans if s.get("span_kind") == "AGENT"]
    for s in agent_spans:
        for ev in s.get("events", []):
            if ev.get("name") == "gen_ai.choice":
                ev_attrs = ev.get("attributes", {})
                if ev_attrs.get("finish_reason") == "end_turn":
                    msg = ev_attrs.get("message", "")
                    # AGENT span choice.message is raw text, not a JSON array.
                    final_output = msg if isinstance(msg, str) else _parse_event_content(msg)
                    break
        if final_output:
            break

    if not final_output:
        for span in reversed(llm_spans):
            for ev in span.get("events", []):
                if ev.get("name") == "gen_ai.choice":
                    ev_attrs = ev.get("attributes", {})
                    if ev_attrs.get("finish_reason") == "end_turn":
                        final_output = _parse_event_content(ev_attrs.get("message", ""))
                        break
            if final_output:
                break

    if final_output:
        lines.append("**Final output:**")
        if len(final_output) > 2000:
            lines.append(f"*[…{len(final_output) - 2000} chars…]*")
            lines.append("")
            lines.append(final_output[-2000:])
        else:
            lines.append(final_output)
        lines.append("")
    elif not lines:
        lines.append(f"*{len(spans)} span(s) recorded. Could not extract conversation.*")
        for s in spans[:10]:
            lines.append(f"- `{s.get('name', '?')}` ({s.get('span_kind', '?')})")
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
