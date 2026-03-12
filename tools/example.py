"""
Example MCP tools demonstrating the tool definition pattern.

Each tool needs:
    1. A definition dict with name, description, and inputSchema (JSON Schema)
    2. An async handler function that receives the tool arguments as kwargs

Export:
    TOOLS    - List of tool definition dicts (for tools/list)
    HANDLERS - Dict mapping tool name -> async handler function (for tools/call)
"""

import platform
from datetime import datetime, timezone
from typing import Dict, Any, Callable, Coroutine


# ===========================================================================
# Tool Definitions (JSON Schema for MCP tools/list)
# ===========================================================================

ECHO_TOOL = {
    "name": "echo",
    "description": "Returns the input text unchanged. Useful for testing connectivity.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back.",
            },
        },
        "required": ["text"],
    },
}

HELLO_WORLD_TOOL = {
    "name": "hello_world",
    "description": "Greets a person by name with an optional custom message.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the person to greet.",
            },
            "message": {
                "type": "string",
                "description": "Optional custom greeting message. Defaults to 'Hello'.",
            },
        },
        "required": ["name"],
    },
}

GET_STATUS_TOOL = {
    "name": "get_status",
    "description": "Returns current server status including uptime, platform, and tool count.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ===========================================================================
# Tool Handlers (async functions called by tools/call)
# ===========================================================================

_start_time = datetime.now(timezone.utc)


async def handle_echo(text: str = "") -> str:
    """Echo handler - returns the input text unchanged."""
    return text


async def handle_hello_world(name: str = "World", message: str = "Hello") -> str:
    """Hello world handler - greets a person by name."""
    return f"{message}, {name}! Welcome to the MCP server."


async def handle_get_status() -> str:
    """Status handler - returns server information."""
    now = datetime.now(timezone.utc)
    uptime = now - _start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    from tools import ALL_TOOLS

    status_lines = [
        f"Server Status: OK",
        f"Time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Uptime: {hours}h {minutes}m {seconds}s",
        f"Platform: {platform.system()} {platform.release()}",
        f"Python: {platform.python_version()}",
        f"Tools registered: {len(ALL_TOOLS)}",
    ]
    return "\n".join(status_lines)


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [ECHO_TOOL, HELLO_WORLD_TOOL, GET_STATUS_TOOL]

HANDLERS: Dict[str, Callable[..., Coroutine]] = {
    "echo": handle_echo,
    "hello_world": handle_hello_world,
    "get_status": handle_get_status,
}
