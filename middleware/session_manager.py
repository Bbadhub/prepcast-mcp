"""
Session Manager Middleware — MCP SaaS Template

Tracks active MCP sessions in memory (ephemeral, no persistence required).
Each session records which tools have been called and carries a small
context dict that tools can read and write for cross-tool state.

No I/O, no external dependencies.  Always succeeds.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """
    In-memory session store.

    Session record schema:
        session_id     (str)   — the MCP session ID
        user_id        (str)   — extracted from API key / request
        created_at     (str)   — ISO-8601 UTC timestamp
        last_seen      (str)   — ISO-8601 UTC timestamp, updated on each tool call
        completed_tools (list) — ordered list of tool names called this session
        current_context (dict) — arbitrary key/value state tools may share
    """

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create_session(
        self,
        session_id: str,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        """
        Return the existing session or create a new one.

        Never raises — returns a valid dict in all cases.
        """
        if not session_id:
            # Caller didn't supply a session ID; return a transient dict
            return self._new_record("_transient", user_id)

        if session_id not in self._sessions:
            self._sessions[session_id] = self._new_record(session_id, user_id)
            logger.debug("session_manager: new session %s (user=%s)", session_id, user_id)

        return self._sessions[session_id]

    def update_session(
        self,
        session_id: str,
        tool_name: str,
        result_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Record a completed tool call on the session.

        Updates last_seen, appends tool to completed_tools, and optionally
        merges result_context into current_context.
        """
        if not session_id or session_id not in self._sessions:
            return

        session = self._sessions[session_id]
        session["last_seen"] = _now_iso()
        session["completed_tools"].append(tool_name)

        if result_context:
            session["current_context"].update(result_context)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the session dict or None if it doesn't exist."""
        return self._sessions.get(session_id)

    def cleanup_stale_sessions(self, max_age_minutes: int = 60) -> int:
        """
        Remove sessions that have not been seen within max_age_minutes.

        Returns the number of sessions removed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        stale = []

        for sid, session in self._sessions.items():
            try:
                last_seen = datetime.fromisoformat(session["last_seen"])
                if last_seen < cutoff:
                    stale.append(sid)
            except (KeyError, ValueError):
                stale.append(sid)

        for sid in stale:
            del self._sessions[sid]

        if stale:
            logger.debug("session_manager: cleaned up %d stale sessions", len(stale))

        return len(stale)

    def active_count(self) -> int:
        """Return the number of currently tracked sessions."""
        return len(self._sessions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _new_record(session_id: str, user_id: str) -> Dict[str, Any]:
        now = _now_iso()
        return {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now,
            "last_seen": now,
            "completed_tools": [],
            "current_context": {},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Return the global SessionManager singleton."""
    global _instance
    if _instance is None:
        _instance = SessionManager()
    return _instance
