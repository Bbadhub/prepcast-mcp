"""
Signal Capture Middleware — MCP SaaS Template

Captures every tool call as a signal: intent, timing, success.
Persists to a JSON file asynchronously (fire-and-forget).
Provides trending and stats queries over the captured signal store.

Environment Variables:
    SIGNAL_CAPTURE_ENABLED  - "true"/"false" (default: true)
    SIGNAL_LOG_PATH         - Path to signals JSON file
                              (default: /data/signals/signals.json)
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("SIGNAL_CAPTURE_ENABLED", "true").lower() not in ("false", "0", "no")
_DEFAULT_LOG_PATH = "/data/signals/signals.json"

# Keys we scan (in order) to extract a human-readable search term from arguments
_SEARCH_KEYS = ("query", "search", "keyword", "q", "term", "name", "title")


def _extract_search_term(arguments: Dict[str, Any]) -> str:
    """Return the first string value found under any recognised search key."""
    for key in _SEARCH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# SignalCapture
# ---------------------------------------------------------------------------

class SignalCapture:
    """
    Thread-safe, fail-safe signal recorder.

    All writes are fire-and-forget via asyncio.create_task so they never
    block the tool response.  The backing store is a newline-delimited JSON
    file (each line is one JSON object) so appends are O(1).
    """

    def __init__(self, log_path: str = _DEFAULT_LOG_PATH):
        self._log_path = Path(log_path)
        self._enabled = _ENABLED
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str,
        session_id: str,
        duration_ms: float,
        success: bool,
    ) -> None:
        """
        Record one tool-call signal.  Non-blocking: schedules a background
        task and returns immediately.
        """
        if not self._enabled:
            return

        signal = {
            "tool_name": tool_name,
            "search_term": _extract_search_term(arguments),
            "user_id": user_id,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "success": success,
        }

        # Fire-and-forget — never await this in the hot path
        asyncio.create_task(self._write(signal))

    def get_trending(self, n: int = 10) -> List[Dict[str, Any]]:
        """
        Return the top-N search terms from the last 7 days.

        Returns a list of dicts: [{term, count}, ...]
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        counts: Dict[str, int] = {}

        for signal in self._iter_signals():
            term = signal.get("search_term", "").strip()
            if not term:
                continue
            ts_raw = signal.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            counts[term] = counts.get(term, 0) + 1

        sorted_terms = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{"term": t, "count": c} for t, c in sorted_terms[:n]]

    def get_signal_stats(self) -> Dict[str, Any]:
        """
        Return aggregate stats over the full signal store.

        Returns:
            total_signals, unique_users, top_tools, top_search_terms
        """
        total = 0
        users: set = set()
        tool_counts: Dict[str, int] = {}
        term_counts: Dict[str, int] = {}

        for signal in self._iter_signals():
            total += 1
            uid = signal.get("user_id", "")
            if uid:
                users.add(uid)
            tool = signal.get("tool_name", "")
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            term = signal.get("search_term", "").strip()
            if term:
                term_counts[term] = term_counts.get(term, 0) + 1

        top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        top_terms = sorted(term_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "total_signals": total,
            "unique_users": len(users),
            "top_tools": [{"tool": t, "count": c} for t, c in top_tools],
            "top_search_terms": [{"term": t, "count": c} for t, c in top_terms],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _write(self, signal: Dict[str, Any]) -> None:
        """Append one signal to the NDJSON log file (async, locked)."""
        try:
            async with self._lock:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(signal) + "\n"
                # Append mode — O(1) write regardless of file size
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:
            # Middleware must never crash the server
            logger.debug("signal_capture: write failed", exc_info=True)

    def _iter_signals(self):
        """Yield each signal dict from the NDJSON log file."""
        if not self._log_path.exists():
            return
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            logger.debug("signal_capture: read failed", exc_info=True)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SignalCapture] = None


def get_signal_capture(log_path: Optional[str] = None) -> SignalCapture:
    """Return the global SignalCapture singleton."""
    global _instance
    if _instance is None:
        path = log_path or os.environ.get("SIGNAL_LOG_PATH", _DEFAULT_LOG_PATH)
        _instance = SignalCapture(log_path=path)
    return _instance
