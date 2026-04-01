"""Minimal example STDIO MCP server for testing local MCP plumbing.

Run standalone:
    uv run python example_mcp_server.py

Reference in local-mcp-servers.json:
    {
      "mcpServers": {
        "my-local-tool": {
          "command": "uv",
          "args": ["run", "python", "/app/agent-runner/agent_runner/example_mcp_server.py"]
        }
      }
    }
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-local-tool")


@mcp.tool()
def hello_world() -> str:
    """Say hello world."""
    return "hello world"


if __name__ == "__main__":
    mcp.run()
