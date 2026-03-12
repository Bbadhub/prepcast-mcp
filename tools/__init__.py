"""
PrepCast Tool Registry.

Aggregates all tool definitions and handlers.
"""

from typing import Dict, List, Any, Callable, Coroutine

from tools.example import TOOLS as EXAMPLE_TOOLS, HANDLERS as EXAMPLE_HANDLERS
from tools.sales_forecast import TOOLS as FORECAST_TOOLS, HANDLERS as FORECAST_HANDLERS
from tools.prep_list import TOOLS as PREP_TOOLS, HANDLERS as PREP_HANDLERS
from tools.events import TOOLS as EVENT_TOOLS, HANDLERS as EVENT_HANDLERS
from tools.upload_report import TOOLS as UPLOAD_TOOLS, HANDLERS as UPLOAD_HANDLERS
from tools.weather import TOOL_GET_WEATHER_FORECAST, handle_get_weather_forecast

ALL_TOOLS: List[Dict[str, Any]] = [
    *FORECAST_TOOLS,   # forecast_sales, analyze_history
    *PREP_TOOLS,       # generate_prep_list, update_menu_ratios, get_menu_ratios
    *EVENT_TOOLS,      # get_upcoming_events, log_event_outcome, get_event_multipliers
    *UPLOAD_TOOLS,     # upload_sales_csv, log_daily_sales, get_sales_summary
    TOOL_GET_WEATHER_FORECAST,
    *EXAMPLE_TOOLS,    # echo, hello_world, get_status
]

ALL_HANDLERS: Dict[str, Callable[..., Coroutine]] = {
    **FORECAST_HANDLERS,
    **PREP_HANDLERS,
    **EVENT_HANDLERS,
    **UPLOAD_HANDLERS,
    "get_weather_forecast": handle_get_weather_forecast,
    **EXAMPLE_HANDLERS,
}
