"""Test local STDIO MCP servers defined in local-mcp-servers.json.

Usage (inside the agent-runner container):
    uv run test-local-mcp

For each server in local-mcp-servers.json, this script:
  1. Spawns the STDIO subprocess
  2. Initialises the MCP session
  3. Lists available tools
  4. Prints a summary and exits non-zero if any server failed
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

LOCAL_MCP_CONFIG_PATH = os.environ.get("LOCAL_MCP_CONFIG_PATH", "/app/local-mcp-servers.json")


def _load_config() -> dict[str, dict]:
    if not os.path.exists(LOCAL_MCP_CONFIG_PATH):
        print(f"ERROR: config file not found: {LOCAL_MCP_CONFIG_PATH}")
        sys.exit(1)
    with open(LOCAL_MCP_CONFIG_PATH) as f:
        data = json.load(f)
    servers = data.get("mcpServers", {})
    if not servers:
        print("No servers defined in mcpServers — nothing to test.")
        sys.exit(0)
    return servers


async def _test_server(name: str, cfg: dict) -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = cfg.get("command")
    if not command:
        print(f"  [{name}] SKIP — no 'command' defined")
        return False

    args = cfg.get("args", [])
    env = {**os.environ, **cfg.get("env", {})}
    params = StdioServerParameters(command=command, args=args, env=env)

    print(f"  [{name}] starting: {command} {' '.join(args)}")
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tool_names = [t.name for t in result.tools]
                print(f"  [{name}] OK — {len(tool_names)} tool(s): {', '.join(tool_names) or '(none)'}")
                return True
    except Exception as exc:
        print(f"  [{name}] FAIL — {exc}")
        return False


async def _run() -> None:
    servers = _load_config()
    print(f"Testing {len(servers)} local MCP server(s) from {LOCAL_MCP_CONFIG_PATH}\n")

    results = {}
    for name, cfg in servers.items():
        results[name] = await _test_server(name, cfg)

    passed = sum(results.values())
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} server(s) passed.")
    if failed:
        sys.exit(1)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
