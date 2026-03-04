"""
MCP (Model Context Protocol) client — Streamable HTTP transport.

Supports the 2024-11-05 protocol version.
Handles both application/json and text/event-stream response bodies.
Tracks the Mcp-Session-Id header returned by the server.
"""

import json
from typing import Any, Optional

import httpx


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class MCPClient:
    """
    Sends JSON-RPC 2.0 requests to a remote MCP server using Bearer auth.

    Usage::

        client = MCPClient("https://mcp.linear.app/mcp", access_token)
        server_info = await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("listIssues", {"teamId": "..."})
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, server_url: str, access_token: str) -> None:
        self.server_url = server_url
        self.access_token = access_token
        self.session_id: Optional[str] = None
        self._request_id = 0

    # ------------------------------------------------------------------
    # Public MCP methods
    # ------------------------------------------------------------------

    async def initialize(self) -> dict:
        """Send the MCP initialize handshake and return the server's capabilities."""
        result = await self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "mcp-oauth-test", "version": "1.0.0"},
            },
        )
        # Send the required initialized notification (fire-and-forget)
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list")
        return (result or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})

    async def list_resources(self) -> list[dict]:
        result = await self._request("resources/list")
        return (result or {}).get("resources", [])

    async def list_prompts(self) -> list[dict]:
        result = await self._request("prompts/list")
        return (result or {}).get("prompts", [])

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _request(self, method: str, params: Optional[dict] = None) -> Any:
        """Send a JSON-RPC request and return the unwrapped result."""
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params

        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                self.server_url,
                json=payload,
                headers=self._headers(),
            )

        # Capture session ID for subsequent requests
        if "mcp-session-id" in response.headers:
            self.session_id = response.headers["mcp-session-id"]

        if response.status_code == 401:
            raise MCPError(-32001, "Unauthorized — token may be expired or invalid")

        if response.status_code == 404:
            raise MCPError(-32000, f"MCP endpoint not found: {self.server_url}")

        if response.status_code not in (200, 202):
            raise MCPError(
                -32000,
                f"Unexpected HTTP {response.status_code}: {response.text[:300]}",
            )

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(response.text)
        if "application/json" in content_type:
            return self._unwrap(response.json())

        # Attempt JSON parse even if content-type is wrong
        try:
            return self._unwrap(response.json())
        except Exception:
            raise MCPError(
                -32000,
                f"Unexpected content-type {content_type!r}: {response.text[:200]}",
            )

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.post(self.server_url, json=payload, headers=self._headers())
        except httpx.RequestError:
            pass  # Notifications are fire-and-forget

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _unwrap(self, data: dict) -> Any:
        """Extract result from a JSON-RPC envelope, or raise MCPError."""
        if "error" in data:
            err = data["error"]
            raise MCPError(
                err.get("code", -32000),
                err.get("message", "Unknown error"),
                err.get("data"),
            )
        return data.get("result")

    def _parse_sse(self, text: str) -> Any:
        """Parse the first JSON-RPC response from an SSE body."""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str and data_str != "[DONE]":
                    try:
                        return self._unwrap(json.loads(data_str))
                    except (json.JSONDecodeError, MCPError):
                        raise
        raise MCPError(-32000, "No valid JSON-RPC response in SSE stream")
