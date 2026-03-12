"""
Middleware package for MCP SaaS Template.

Provides the data flywheel: every tool call captures a signal,
enriches the result, and feeds future calls.

Components:
    signal_capture   - Fire-and-forget signal recording to JSON store
    serper_connector - Web enrichment via Serper API (cached, fail-safe)
    session_manager  - In-memory session state tracking
    sync             - Two-way sync engine tying all middleware together
"""

from middleware.signal_capture import SignalCapture, get_signal_capture
from middleware.serper_connector import SerperConnector, get_serper
from middleware.session_manager import SessionManager, get_session_manager
from middleware.sync import SyncEngine, get_sync_engine

__all__ = [
    "SignalCapture",
    "get_signal_capture",
    "SerperConnector",
    "get_serper",
    "SessionManager",
    "get_session_manager",
    "SyncEngine",
    "get_sync_engine",
]
