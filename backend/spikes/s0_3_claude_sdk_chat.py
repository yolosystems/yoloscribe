"""S0.3 spike — Claude Agent SDK chat driven through the real YoloScribe MCP
wiki tools, with Yolo Brain memory injected via a harness-side resource
fetch + system_prompt (NOT SDK-native resource injection — the Claude Agent
SDK's MCP integration is tools-only as of 2026-07, confirmed against the
official docs; there is no list_resources/read_resource API and no
auto-injection of MCP resource content into context).

See projects/yoloscribe/feature-backlog/agent-runtime-rearchitecture (S0.3)
and projects/yolo-brain/implementation-plan ("Future: Ambient Memory
Context") in the wiki.

Usage (from backend/, with the spike dependency group installed):
    uv sync --group spike
    uv run python spikes/s0_3_claude_sdk_chat.py --via 1p
    uv run python spikes/s0_3_claude_sdk_chat.py --via bedrock

Requires a local backend running with LOCAL_MODE=true (see docker-compose.yml
/ INSTALL.md) reachable at S0_3_MCP_URL (default http://localhost:8000/mcp/v1),
and either ANTHROPIC_API_KEY in the environment (--via 1p) or AWS credentials
with Bedrock access (--via bedrock).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("S0_3_MCP_URL", "http://localhost:8000/mcp/v1")
MCP_TOKEN = os.environ.get("S0_3_MCP_TOKEN", "local")

SCRATCH_PAGE = "spike-notes"
MEMORY_CONCLUSION_ID = "c-s0-3-spike"
CONVENTION_STATEMENT = (
    "Project pages always get a '## Status' section right after the title "
    "and a '## Next Steps' section at the end."
)
SEED_CONTENT = "# Spike Notes\n\n(placeholder -- to be structured by the chat agent)\n"
USER_PROMPT = (
    "Please clean up and structure the wiki page at 'spike-notes'. It currently just "
    "has a placeholder. Write a short page about today's spike: we validated the "
    "Claude Agent SDK against the YoloScribe MCP server and it worked well."
)

RESULTS_PATH = Path(__file__).parent / "S0_3_RESULTS.md"


async def _mcp_session(fn):
    async with streamablehttp_client(MCP_URL, headers={"Authorization": f"Bearer {MCP_TOKEN}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def seed_memory_and_page() -> None:
    async def _do(session: ClientSession):
        await session.call_tool(
            "write_memory",
            {
                "conclusions": [
                    {
                        "id": MEMORY_CONCLUSION_ID,
                        "level": "explicit",
                        "domain": "present",
                        "statement": CONVENTION_STATEMENT,
                    }
                ]
            },
        )
        try:
            await session.call_tool("wiki_create", {"page_path": SCRATCH_PAGE, "content": SEED_CONTENT})
        except Exception:
            # Already exists from a prior run of this spike -- reset it.
            await session.call_tool("wiki_update", {"page_path": SCRATCH_PAGE, "content": SEED_CONTENT})

    await _mcp_session(_do)


async def fetch_resources() -> tuple[str, str]:
    async def _do(session: ClientSession):
        memory = await session.read_resource("memory://current")
        page_index = await session.read_resource("page-index://current")
        return memory.contents[0].text, page_index.contents[0].text

    return await _mcp_session(_do)


async def read_page() -> str:
    async def _do(session: ClientSession):
        result = await session.call_tool("wiki_read", {"page_path": SCRATCH_PAGE})
        return result.content[0].text if result.content else ""

    return await _mcp_session(_do)


def build_system_prompt(memory_json: str, page_index_json: str) -> str:
    return (
        "You are the YoloScribe wiki assistant. You have wiki_read and wiki_update "
        "tools scoped to this site's pages.\n\n"
        "The following is ambient context about how this wiki's owner likes things "
        "structured -- it was fetched automatically, not something you were told to "
        "go look up:\n\n"
        f"MEMORY (Librarian preference conclusions):\n{memory_json}\n\n"
        f"PAGE INDEX (existing pages on this site):\n{page_index_json}\n"
    )


def via_env(via: str) -> dict[str, str]:
    if via == "bedrock":
        return {"CLAUDE_CODE_USE_BEDROCK": "1"}
    return {}


async def run_conversation(via: str) -> dict:
    system_prompt = build_system_prompt(*await fetch_resources())

    options = ClaudeAgentOptions(
        mcp_servers={
            "yoloscribe": {
                "type": "http",
                "url": MCP_URL,
                "headers": {"Authorization": f"Bearer {MCP_TOKEN}"},
            }
        },
        allowed_tools=["mcp__yoloscribe__wiki_read", "mcp__yoloscribe__wiki_update"],
        system_prompt=system_prompt,
        env=via_env(via),
    )

    tool_calls: list[str] = []
    final_text = ""
    result: ResultMessage | None = None

    async with ClaudeSDKClient(options=options) as client:
        await client.query(USER_PROMPT)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls.append(f"{block.name}({json.dumps(block.input)[:200]})")
                    elif isinstance(block, TextBlock):
                        final_text += block.text
            if isinstance(message, ResultMessage):
                result = message

    return {
        "system_prompt": system_prompt,
        "tool_calls": tool_calls,
        "final_text": final_text,
        "result": result,
    }


def render_results(via: str, run: dict, final_page: str) -> str:
    result: ResultMessage | None = run["result"]
    called_wiki_update = any("wiki_update(" in c for c in run["tool_calls"])
    has_status = "## Status" in final_page
    has_next_steps = "## Next Steps" in final_page

    lines = [
        "# S0.3 Spike Results",
        "",
        f"_Run: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}, via={via}_",
        "",
        "## Objective checks",
        "",
        f"- Edit went through the MCP tool (`wiki_update` actually called): "
        f"**{'yes' if called_wiki_update else 'NO'}**",
        f"- Memory-derived `## Status` section present without being asked for it: "
        f"**{'yes' if has_status else 'no'}**",
        f"- Memory-derived `## Next Steps` section present without being asked for it: "
        f"**{'yes' if has_next_steps else 'no'}**",
        "",
        "## Cost / latency",
        "",
    ]
    if result is not None:
        lines += [
            f"- `num_turns`: {result.num_turns}",
            f"- `duration_ms`: {result.duration_ms} (api: {result.duration_api_ms})",
            f"- `total_cost_usd`: {result.total_cost_usd}",
            f"- `usage`: `{json.dumps(result.usage)}`",
            f"- `is_error`: {result.is_error}",
        ]
    else:
        lines.append("- No ResultMessage received.")

    lines += [
        "",
        "## Tool calls made",
        "",
        "```",
        *(run["tool_calls"] or ["(none)"]),
        "```",
        "",
        "## System prompt used (ambient memory injection)",
        "",
        "```",
        run["system_prompt"],
        "```",
        "",
        "## Final assistant text",
        "",
        "```",
        run["final_text"] or "(empty)",
        "```",
        "",
        "## Final page content (`spike-notes`)",
        "",
        "```markdown",
        final_page,
        "```",
        "",
        "## Not self-graded",
        "",
        "\"Interactive quality judged >= the current chatbot\" is a qualitative call "
        "left for human review of the transcript above, not asserted here.",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--via", choices=["1p", "bedrock"], default="1p")
    args = parser.parse_args()

    print(f"[s0.3] seeding memory conclusion + scratch page (via={args.via})...")
    await seed_memory_and_page()

    print("[s0.3] running conversation...")
    run = await run_conversation(args.via)

    print("[s0.3] re-reading final page...")
    final_page = await read_page()

    report = render_results(args.via, run, final_page)
    RESULTS_PATH.write_text(report)
    print(f"[s0.3] wrote {RESULTS_PATH}")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
