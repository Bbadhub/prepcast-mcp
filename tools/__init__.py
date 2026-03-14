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
from tools.kpi_report import TOOLS as KPI_TOOLS, HANDLERS as KPI_HANDLERS

ALL_TOOLS: List[Dict[str, Any]] = [
    *FORECAST_TOOLS,   # forecast_sales, analyze_history
    *PREP_TOOLS,       # generate_prep_list, log_prep_outcome, get_prep_dashboard, update_menu_ratios, get_menu_ratios
    *EVENT_TOOLS,      # get_upcoming_events, log_event_outcome, get_event_multipliers
    *UPLOAD_TOOLS,     # upload_sales_data, upload_sales_csv, log_daily_sales, get_sales_summary
    TOOL_GET_WEATHER_FORECAST,
    *KPI_TOOLS,        # get_performance_report, get_forecast_accuracy, get_revenue_trends
    *EXAMPLE_TOOLS,    # echo, hello_world, get_status
]

ALL_HANDLERS: Dict[str, Callable[..., Coroutine]] = {
    **FORECAST_HANDLERS,
    **PREP_HANDLERS,
    **EVENT_HANDLERS,
    **UPLOAD_HANDLERS,
    "get_weather_forecast": handle_get_weather_forecast,
    **KPI_HANDLERS,
    **EXAMPLE_HANDLERS,
}
