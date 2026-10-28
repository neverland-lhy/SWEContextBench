"""MCP server exposing a mem0-style tool surface.

Tool names, parameters and return shapes mirror the official Mem0 MCP plugin
so the agent behaves identically to using stock mem0 — only the backend swaps.
The bridge does NO LLM / rerank; everything is forwarded to the backend.
"""

from __future__ import annotations

import json
from typing import Any

from .protocol import Memory, get_backend

# Tools we expose. Keep names/params aligned with mem0-mcp.
TOOLS = [
    {
        "name": "add_memory",
        "description": (
            "Save text or conversation history for a user. "
            "Pass `content` (string) or `messages` (list of "
            "{role, content}). `user_id` scopes retrieval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
                "user_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Semantic search across memories for a query, scoped to user_id. "
            "Call this BEFORE fixing a bug, passing the problem statement as "
            "the query, to retrieve relevant past experience."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string"},
                "topK": {"type": "integer", "default": 5},
                "limit": {"type": "integer"},
                "filters": {"type": "object"},
                "threshold": {"type": "number"},
            },
            "required": ["query", "user_id"],
        },
    },
    {
        "name": "get_memories",
        "description": "List memories for a user (paginated).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "get_memory",
        "description": "Retrieve one memory by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "update_memory",
        "description": "Overwrite a memory's text by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["memory_id", "content"],
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete one memory by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
]


def _mem_to_dict(m: Memory) -> dict[str, Any]:
    return {
        "id": m.id,
        "memory": m.content,
        "content": m.content,
        "score": m.score,
        "created_at": m.created_at,
        "metadata": m.metadata,
    }


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class BridgeServer:
    """Transport-agnostic handler. `mcp_server` adapts this to MCP stdio."""

    def __init__(self, backend_name: str, **backend_kwargs: Any) -> None:
        self.backend = get_backend(backend_name, **backend_kwargs)

    def list_tools(self) -> list[dict]:
        return TOOLS

    def call_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "add_memory":
            content = args.get("content")
            messages = args.get("messages")
            if not messages and content:
                messages = [{"role": "user", "content": content}]
            if not messages:
                return _json({"error": "nothing to add"})
            res = self.backend.add(
                request_id=f"mcp-{name}-{id(messages)}",
                messages=messages,
                user_id=args.get("user_id", "default"),
                session_id=args.get("agent_id", args.get("user_id", "default")),
            )
            return _json(res)

        if name == "search_memories":
            top_k = args.get("topK") or args.get("limit") or 5
            results = self.backend.search(
                query=args["query"],
                user_id=args["user_id"],
                top_k=int(top_k),
            )
            return _json({"results": [_mem_to_dict(m) for m in results]})

        if name == "get_memories":
            mems = self.backend.list_memories(
                user_id=args["user_id"], limit=args.get("limit", 100)
            )
            return _json({"results": [_mem_to_dict(m) for m in mems]})

        if name == "get_memory":
            m = self.backend.get(args["memory_id"])
            return _json(_mem_to_dict(m) if m else {"error": "not found"})

        if name == "update_memory":
            m = self.backend.update(args["memory_id"], args["content"])
            return _json(_mem_to_dict(m) if m else {"success": True})

        if name == "delete_memory":
            ok = self.backend.delete(args["memory_id"])
            return _json({"success": ok})

        return _json({"error": f"unknown tool {name}"})


def run_stdio(backend_name: str, **backend_kwargs: Any) -> None:
    """Run the MCP server over stdio (JSON-RPC). Uses mcp 2.x MCPServer."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import Tool

    server = BridgeServer(backend_name, **backend_kwargs)
    mcp = MCPServer("memory-bridge")

    @mcp.tool()
    def add_memory(
        content: str | None = None,
        messages: list[dict] | None = None,
        user_id: str = "default",
        agent_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Save text or conversation history for a user.

        Pass `content` (string) or `messages` (list of {role, content}).
        `user_id` scopes retrieval. Call this AFTER solving a task to record
        reusable experience.
        """
        if not messages and content:
            messages = [{"role": "user", "content": content}]
        if not messages:
            return _json({"error": "nothing to add"})
        return server.call_tool("add_memory", {
            "content": content, "messages": messages,
            "user_id": user_id, "agent_id": agent_id, "metadata": metadata,
        })

    @mcp.tool()
    def search_memories(
        query: str,
        user_id: str,
        topK: int = 5,
        limit: int | None = None,
    ) -> str:
        """Semantic search across memories, scoped to user_id.

        Call this BEFORE fixing a bug, passing the problem statement as the
        query, to retrieve relevant past experience.
        """
        return server.call_tool("search_memories", {
            "query": query, "user_id": user_id,
            "topK": topK, "limit": limit,
        })

    @mcp.tool()
    def get_memories(user_id: str, limit: int = 100) -> str:
        """List memories for a user (paginated)."""
        return server.call_tool("get_memories", {"user_id": user_id, "limit": limit})

    @mcp.tool()
    def get_memory(memory_id: str) -> str:
        """Retrieve one memory by id."""
        return server.call_tool("get_memory", {"memory_id": memory_id})

    @mcp.tool()
    def update_memory(memory_id: str, content: str) -> str:
        """Overwrite a memory's text by id."""
        return server.call_tool("update_memory", {"memory_id": memory_id, "content": content})

    @mcp.tool()
    def delete_memory(memory_id: str) -> str:
        """Delete one memory by id."""
        return server.call_tool("delete_memory", {"memory_id": memory_id})

    import asyncio

    asyncio.run(mcp.run_stdio_async())
