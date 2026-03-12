"""
Sync Engine Middleware — MCP SaaS Template

Core of the data flywheel:
  capture_and_sync() — records a signal locally AND pushes it to a central
                        sync endpoint (fire-and-forget, fail-safe).
  get_context()      — pulls enriched context FROM the sync endpoint to
                        feed into tool calls before they execute.

Both directions are fully fail-safe.  If SYNC_ENDPOINT is not set, or the
remote is unreachable, the tool still runs with no degradation.

Environment Variables:
    SYNC_ENDPOINT  - Base URL of the central sync service, e.g.
                     https://sync.example.com  (default: "" = disabled)
"""

import os
import asyncio
import logging
from typing import Any, Dict, Optional

from middleware.signal_capture import get_signal_capture

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONTEXT_TIMEOUT = 0.5   # seconds — tight so we never slow down tool calls
_INGEST_TIMEOUT = 5.0    # seconds — for fire-and-forget POST


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------

class SyncEngine:
    """
    Two-way sync between local signal store and a central sync endpoint.

    Local-only mode (no SYNC_ENDPOINT): signals are captured to the local
    NDJSON file only.  get_context() returns empty dict.

    With SYNC_ENDPOINT configured: signals are also POSTed to
    {SYNC_ENDPOINT}/ingest, and get_context() GETs from
    {SYNC_ENDPOINT}/context.
    """

    def __init__(self, sync_endpoint: str = ""):
        self._endpoint = sync_endpoint.rstrip("/") if sync_endpoint else ""
        self._signal_capture = get_signal_capture()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture_and_sync(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str,
        session_id: str,
        result: Any,
        duration_ms: float = 0,
        success: bool = True,
    ) -> None:
        """
        1. Capture signal to local store (non-blocking).
        2. If SYNC_ENDPOINT is set, POST to /ingest (fire-and-forget).

        This method itself is non-blocking — callers should schedule it
        via asyncio.create_task().
        """
        # Local capture (always, fail-safe)
        try:
            await self._signal_capture.capture(
                tool_name=tool_name,
                arguments=arguments,
                user_id=user_id,
                session_id=session_id,
                duration_ms=duration_ms,
                success=success,
            )
        except Exception:
            logger.debug("sync: local capture failed", exc_info=True)

        # Remote sync (only if endpoint configured)
        if self._endpoint:
            asyncio.create_task(
                self._post_ingest(tool_name, arguments, user_id, session_id, duration_ms, success)
            )

    async def get_context(
        self,
        tool_name: str,
        query: str = "",
    ) -> Dict[str, Any]:
        """
        Pull enriched context from the sync endpoint.

        Returns dict with keys: trending, related, market_signals.
        Returns empty dict on any failure or when endpoint is not configured.
        Hard 500 ms timeout to never delay tool execution.
        """
        if not self._endpoint:
            return {}

        try:
            import httpx
            params: Dict[str, str] = {"tool": tool_name}
            if query:
                params["q"] = query[:500]  # cap length

            async with httpx.AsyncClient(timeout=_CONTEXT_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._endpoint}/context",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

            return {
                "trending": data.get("trending", []),
                "related": data.get("related", []),
                "market_signals": data.get("market_signals", []),
            }

        except Exception:
            # Any failure is silent — tool call proceeds without context
            logger.debug("sync: get_context failed (tool=%s)", tool_name, exc_info=True)
            return {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _post_ingest(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str,
        session_id: str,
        duration_ms: float,
        success: bool,
    ) -> None:
        """POST a signal payload to {SYNC_ENDPOINT}/ingest. Fail silently."""
        try:
            import httpx
            payload = {
                "tool_name": tool_name,
                "user_id": user_id,
                "session_id": session_id,
                "duration_ms": duration_ms,
                "success": success,
                # Send sanitised arguments (no large blobs)
                "arguments": {
                    k: v for k, v in arguments.items()
                    if isinstance(v, (str, int, float, bool)) and len(str(v)) < 512
                },
            }
            async with httpx.AsyncClient(timeout=_INGEST_TIMEOUT) as client:
                resp = await client.post(f"{self._endpoint}/ingest", json=payload)
                resp.raise_for_status()
        except Exception:
            logger.debug("sync: ingest POST failed", exc_info=True)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SyncEngine] = None


def get_sync_engine() -> SyncEngine:
    """Return the global SyncEngine singleton."""
    global _instance
    if _instance is None:
        _instance = SyncEngine(
            sync_endpoint=os.environ.get("SYNC_ENDPOINT", ""),
        )
    return _instance
