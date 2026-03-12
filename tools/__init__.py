"""
Tool Registry for {{SERVER_NAME}}.

Aggregates all tool definitions and handlers from sub-modules.
Import your tool modules here and extend ALL_TOOLS / ALL_HANDLERS.

To add a new tool module:
    1. Create tools/my_tools.py with TOOLS list and HANDLERS dict
    2. Import and extend below
"""

from typing import Dict, List, Any, Callable, Coroutine

from tools.example import TOOLS as EXAMPLE_TOOLS, HANDLERS as EXAMPLE_HANDLERS

# ---------------------------------------------------------------------------
# Aggregate all tool definitions and handler functions
# ---------------------------------------------------------------------------

ALL_TOOLS: List[Dict[str, Any]] = [
    *EXAMPLE_TOOLS,
    # Add more tool lists here as you create new modules:
    # *MY_TOOLS,
]

ALL_HANDLERS: Dict[str, Callable[..., Coroutine]] = {
    **EXAMPLE_HANDLERS,
    # Add more handler dicts here:
    # **MY_HANDLERS,
}
