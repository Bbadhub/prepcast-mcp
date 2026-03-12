"""
{{SERVER_NAME}} - MCP Server (Protocol 2024-11-05)

A generic MCP server supporting both SSE and stdio transports.
Drop in your tools, configure billing, and deploy.

Transports:
    - SSE (Server-Sent Events) over HTTP for remote clients
    - stdio for local Claude Desktop integration

Usage:
    python server.py              # SSE on port {{SERVER_PORT}}
    python server.py --stdio      # stdio transport only
    python server.py --both       # Both SSE + stdio
    python server.py --port 9090  # Custom port
"""

import os
import sys
import json
import asyncio
import argparse
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from aiohttp import web

from config import get_config
from billing import UsageTracker, BillingConfig, create_billing_middleware, add_billing_routes
from tools import ALL_TOOLS, ALL_HANDLERS
from middleware.sync import get_sync_engine
from middleware.session_manager import get_session_manager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("{{SERVER_NAME}}")

# ---------------------------------------------------------------------------
# Server metadata
# ---------------------------------------------------------------------------

SERVER_INFO = {
    "name": "{{SERVER_NAME}}",
    "version": "{{SERVER_VERSION}}",
}

PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Billing setup
# ---------------------------------------------------------------------------

billing_config = BillingConfig()
usage_tracker = UsageTracker(billing_config)
billing_middleware = create_billing_middleware(usage_tracker)


# ===========================================================================
# JSON-RPC MCP Protocol Handler
# ===========================================================================

async def handle_jsonrpc(message: Dict[str, Any], api_key: str = "", session_id: str = "") -> Optional[Dict[str, Any]]:
    """
    Process a single JSON-RPC message according to the MCP protocol.

    Handles:
        - initialize: Capability negotiation
        - notifications/initialized: Client acknowledgment (no response)
        - tools/list: Return available tools
        - tools/call: Execute a tool with billing middleware
        - ping: Health check
    """
    method = message.get("method", "")
    msg_id = message.get("id")
    params = message.get("params", {})

    # --- initialize --------------------------------------------------------
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            },
        }

    # --- notifications (no response) --------------------------------------
    if method.startswith("notifications/"):
        return None

    # --- tools/list --------------------------------------------------------
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": ALL_TOOLS},
        }

    # --- tools/call --------------------------------------------------------
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = ALL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}",
                },
            }

        # --- Middleware: pull enriched context before tool executes --------
        sync_engine = get_sync_engine()
        _context = await sync_engine.get_context(tool_name, str(arguments))
        # Context is available to handlers that accept it; currently informational.
        # Future: inject _context into arguments or handler kwargs as needed.

        # --- Session tracking (pre-call) -----------------------------------
        session_mgr = get_session_manager()
        session_mgr.get_or_create_session(session_id, user_id=api_key or "anonymous")

        import time as _time
        _t0 = _time.monotonic()

        # --- Run through billing middleware (auth, rate limit, metering) ---
        result = await billing_middleware(tool_name, arguments, api_key, handler)

        _duration_ms = (_time.monotonic() - _t0) * 1000
        _success = "error" not in result

        # --- Middleware: capture + sync signal (non-blocking) ---------------
        asyncio.create_task(sync_engine.capture_and_sync(
            tool_name=tool_name,
            arguments=arguments,
            user_id=api_key or "anonymous",
            session_id=session_id,
            result=result,
            duration_ms=_duration_ms,
            success=_success,
        ))

        # --- Session tracking (post-call) ----------------------------------
        session_mgr.update_session(session_id, tool_name)

        # If billing returned an error dict, forward it
        if "error" in result:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": result["error"],
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    # --- ping --------------------------------------------------------------
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    # --- unknown method ----------------------------------------------------
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


# ===========================================================================
# SSE Transport (aiohttp)
# ===========================================================================

class SSETransport:
    """Server-Sent Events transport for remote MCP clients."""

    def __init__(self, app: web.Application):
        self.app = app
        self._clients: Dict[str, web.StreamResponse] = {}

        # MCP SSE endpoints
        app.router.add_get("/sse", self.handle_sse)
        app.router.add_post("/message", self.handle_message)
        app.router.add_post("/messages", self.handle_message)  # Alias

        # Health check
        app.router.add_get("/health", self.handle_health)

    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """
        GET /sse - Establish SSE connection.

        The client connects here and receives an initial 'endpoint' event
        pointing to /message?sessionId=<id> for sending JSON-RPC messages.
        """
        import uuid
        session_id = str(uuid.uuid4())

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await response.prepare(request)

        # Send the endpoint URL the client should POST messages to
        endpoint_url = f"/message?sessionId={session_id}"
        await response.write(f"event: endpoint\ndata: {endpoint_url}\n\n".encode())

        self._clients[session_id] = response
        logger.info("SSE client connected: %s", session_id)

        try:
            # Keep connection alive with periodic heartbeats
            while True:
                await asyncio.sleep(30)
                try:
                    await response.write(b": heartbeat\n\n")
                except (ConnectionResetError, ConnectionError):
                    break
        finally:
            self._clients.pop(session_id, None)
            logger.info("SSE client disconnected: %s", session_id)

        return response

    async def handle_message(self, request: web.Request) -> web.Response:
        """
        POST /message?sessionId=<id> - Receive JSON-RPC message from client.

        The response is sent back both as the HTTP response body AND pushed
        to the SSE stream for the given session.
        """
        session_id = request.query.get("sessionId", "")
        sse_response = self._clients.get(session_id)

        if not sse_response and session_id:
            return web.json_response(
                {"error": "Unknown session"},
                status=404,
            )

        try:
            message = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )

        # Extract API key from headers (for billing)
        api_key = request.headers.get("X-API-Key", request.headers.get("Authorization", ""))
        if api_key.startswith("Bearer "):
            api_key = api_key[7:]

        # Process the JSON-RPC message (session_id already extracted above)
        result = await handle_jsonrpc(message, api_key, session_id)

        if result is None:
            # Notification - no response needed
            return web.Response(status=204)

        # Push result to SSE stream if connected
        if sse_response:
            try:
                sse_data = json.dumps(result)
                await sse_response.write(f"event: message\ndata: {sse_data}\n\n".encode())
            except (ConnectionResetError, ConnectionError):
                self._clients.pop(session_id, None)

        return web.json_response(result)

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health - Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "server": SERVER_INFO["name"],
            "version": SERVER_INFO["version"],
            "protocol": PROTOCOL_VERSION,
            "tools": len(ALL_TOOLS),
            "connected_clients": len(self._clients),
        })


# ===========================================================================
# stdio Transport
# ===========================================================================

async def run_stdio():
    """
    Run the MCP server over stdin/stdout (for Claude Desktop integration).

    Reads JSON-RPC messages from stdin (one per line) and writes responses
    to stdout (one per line).
    """
    logger.info("Starting stdio transport...")

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break

        line = line.decode("utf-8").strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()
            continue

        result = await handle_jsonrpc(message, api_key="stdio-local")

        if result is not None:
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()


# ===========================================================================
# Application Factory & Entry Point
# ===========================================================================

def create_app() -> web.Application:
    """Create the aiohttp application with SSE transport and billing routes."""
    app = web.Application()
    site_dir = Path(__file__).resolve().parent / "site"

    async def handle_landing(_: web.Request) -> web.StreamResponse:
        """Serve bundled landing page if present."""
        index_path = site_dir / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        return web.json_response(
            {
                "service": SERVER_INFO["name"],
                "status": "ok",
                "message": "Landing page not found. Add site/index.html.",
            }
        )

    app.router.add_get("/", handle_landing)

    async def handle_public_comparison(_: web.Request) -> web.StreamResponse:
        comparison = site_dir / "public-comparison.json"
        if comparison.exists():
            return web.FileResponse(comparison)
        return web.json_response(
            {
                "summary": "Run strategy/competitor_analysis.py to generate public-comparison.json.",
                "items": [],
            }
        )

    app.router.add_get("/public-comparison.json", handle_public_comparison)
    if site_dir.exists():
        app.router.add_static("/site/", site_dir, show_index=True)

    async def handle_consumer_tools(_: web.Request) -> web.StreamResponse:
        """Simple browser-friendly listing of tools (no MCP client required)."""
        tools = [
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {}),
            }
            for tool in ALL_TOOLS
        ]
        return web.json_response({"tools": tools, "count": len(tools)})

    async def handle_consumer_run(request: web.Request) -> web.StreamResponse:
        """
        Run a tool through plain HTTP.
        This gives non-MCP users a direct frontend path.
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        tool_name = body.get("tool", "")
        arguments = body.get("arguments", {}) or {}
        if not tool_name:
            return web.json_response({"error": "tool is required"}, status=400)

        handler = ALL_HANDLERS.get(tool_name)
        if not handler:
            return web.json_response({"error": f"Unknown tool: {tool_name}"}, status=404)

        api_key = body.get("api_key", "")
        if not api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
            else:
                api_key = request.headers.get("X-API-Key", "")

        result = await billing_middleware(tool_name, arguments, api_key, handler)
        if "error" in result:
            return web.json_response(result, status=400)
        text = result.get("content", [{}])[0].get("text", "")
        return web.json_response({"ok": True, "tool": tool_name, "result": text})

    app.router.add_get("/consumer/tools", handle_consumer_tools)
    app.router.add_post("/consumer/run", handle_consumer_run)

    # SSE transport (registers /sse, /message, /health)
    SSETransport(app)

    # Billing management routes (/billing/*)
    add_billing_routes(app, usage_tracker)

    # CORS middleware for browser clients
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        return response

    app.middlewares.append(cors_middleware)
    return app


async def run_both(port: int, host: str):
    """Run both SSE and stdio transports concurrently."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("SSE transport listening on http://%s:%d", host, port)

    # Run stdio in parallel
    await run_stdio()

    # Cleanup
    await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="{{SERVER_NAME}} MCP Server")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode (for Claude Desktop)")
    parser.add_argument("--both", action="store_true", help="Run both SSE and stdio transports")
    parser.add_argument("--port", type=int, default=None, help="SSE server port (default: {{SERVER_PORT}})")
    parser.add_argument("--host", type=str, default=None, help="SSE server host (default: 0.0.0.0)")
    args = parser.parse_args()

    config = get_config()
    port = args.port or config.port
    host = args.host or config.host

    logger.info("Starting %s v%s", SERVER_INFO["name"], SERVER_INFO["version"])
    logger.info("Registered tools: %s", [t["name"] for t in ALL_TOOLS])
    logger.info("Billing enabled: %s", billing_config.enabled)

    if args.stdio:
        # stdio only (Claude Desktop)
        asyncio.run(run_stdio())
    elif args.both:
        # Both transports
        asyncio.run(run_both(port, host))
    else:
        # SSE only (default for remote deployment)
        app = create_app()
        logger.info("SSE transport starting on http://%s:%d", host, port)
        web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
