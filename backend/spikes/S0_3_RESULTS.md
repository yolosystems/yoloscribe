# S0.3 Spike Results

_Run: 2026-07-20 20:18 UTC, via=1p_

## Objective checks

- Edit went through the MCP tool (`wiki_update` actually called): **yes**
- Memory-derived `## Status` section present without being asked for it: **yes**
- Memory-derived `## Next Steps` section present without being asked for it: **yes**

## Cost / latency

- `num_turns`: 5
- `duration_ms`: 24281 (api: 25023)
- `total_cost_usd`: 0.24250049999999998
- `usage`: `{"input_tokens": 10, "cache_creation_input_tokens": 22358, "cache_read_input_tokens": 146700, "output_tokens": 1149, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard", "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 22358}, "inference_geo": "global", "iterations": [{"input_tokens": 2, "output_tokens": 237, "cache_read_input_tokens": 36583, "cache_creation_input_tokens": 619, "cache_creation": {"ephemeral_5m_input_tokens": 619, "ephemeral_1h_input_tokens": 0}, "type": "message"}], "speed": "standard"}`
- `is_error`: False

## Tool calls made

```
ToolSearch({"query": "select:wiki_read,wiki_update", "max_results": 5})
ToolSearch({"query": "wiki read update page", "max_results": 10})
mcp__yoloscribe__wiki_read({"page_path": "spike-notes", "include_metadata": true})
mcp__yoloscribe__wiki_update({"page_path": "spike-notes", "content": "# Spike Notes\n\n## Status\n\nComplete \u2014 spike validated successfully on 2026-07-20.\n\n## Summary\n\nToday's spike validated the [Claude Agent SDK](https)
```

## System prompt used (ambient memory injection)

```
You are the YoloScribe wiki assistant. You have wiki_read and wiki_update tools scoped to this site's pages.

The following is ambient context about how this wiki's owner likes things structured -- it was fetched automatically, not something you were told to go look up:

MEMORY (Librarian preference conclusions):
{"owner": "local", "schema_version": 1, "last_consolidated": "2026-07-20T20:17:35Z", "conclusions": [{"id": "c-spike01", "level": "explicit", "domain": "present", "statement": "Project pages always get a Status section right after the title and a Next Steps section at the end.", "status": "active"}, {"id": "c-s0-3-spike", "level": "explicit", "domain": "present", "statement": "Project pages always get a '## Status' section right after the title and a '## Next Steps' section at the end.", "status": "active"}]}

PAGE INDEX (existing pages on this site):
{"nodes": [{"topic": "spike-notes", "pointer": "spike-notes", "system": "yoloscribe", "updated_at": "2026-07-20T20:17:35.487000+00:00"}]}

```

## Final assistant text

```
I'll load the wiki tools and read the current page first.I'll read the current page first.Now I'll write the structured page, following the owner's convention of a Status section right after the title and a Next Steps section at the end.Done. I've replaced the placeholder at `spike-notes` with a structured page.

Following your convention for project-style pages, it opens with a **## Status** section right after the title and closes with a **## Next Steps** section at the end. In between:

- **Summary** — what the spike was: validating the Claude Agent SDK against the YoloScribe MCP server.
- **Outcome** — the SDK connected cleanly, tool discovery/invocation worked, and it worked well with no blockers.
- **Next Steps** — broaden tool coverage, document the setup, and decide whether to promote it beyond a spike.

Happy to adjust the tone or add more detail (e.g. which specific tools you exercised) if you'd like.
```

## Final page content (`spike-notes`)

```markdown
{"page_path":"spike-notes","content":"# Spike Notes\n\n## Status\n\nComplete — spike validated successfully on 2026-07-20.\n\n## Summary\n\nToday's spike validated the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk) against the YoloScribe MCP server. We connected an SDK-driven agent to the server's tools and exercised the wiki read/update paths end to end.\n\n## Outcome\n\n- The Claude Agent SDK connected to the YoloScribe MCP server without issue.\n- Tool discovery and invocation worked as expected against the site-scoped wiki tools.\n- Overall: it worked well, with no blocking problems surfaced during the spike.\n\n## Next Steps\n\n- Broaden coverage to the remaining MCP tools (search, agent, and skill management).\n- Document a repeatable setup for connecting the SDK to the MCP server.\n- Decide whether to promote this from a spike to a tracked piece of work.\n","last_updated":"2026-07-20T20:17:55+00:00","has_proposal":false}
```

## Not self-graded

"Interactive quality judged >= the current chatbot" is a qualitative call left for human review of the transcript above, not asserted here.

## Bedrock leg (`--via bedrock`)

Run separately at 2026-07-20 20:17 UTC. `CLAUDE_CODE_USE_BEDROCK=1` correctly routed the SDK's bundled CLI through Bedrock — confirmed by the failure mode itself: it attempted AWS SSO auth under the `runyolo_admin` profile and failed with `API Error: Token is expired. To refresh this SSO session run 'aws sso login' with the corresponding profile.` before making any model call (`num_turns: 1`, `total_cost_usd: 0`, no tool calls, `is_error: True`).

This confirms the env var mechanism works but the 1P-vs-Bedrock cost/latency comparison couldn't complete — it needs a live `runyolo_admin` SSO session, which is a credential action outside this spike's scope to perform. Re-run `--via bedrock` after `aws sso login --profile runyolo_admin` to get the comparison numbers.
