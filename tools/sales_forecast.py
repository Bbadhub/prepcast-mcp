"""
PrepCast — Sales Forecast Tool

Given a location's historical daily sales data (loaded via upload_report),
project revenue for a target date factoring in:
  - Day-of-week baseline
  - Recent trend (EMA)
  - Known event multipliers (injected from events tool)

Handlers:
    forecast_sales  — project revenue + confidence range for a date
    analyze_history — summarize patterns from stored sales data
"""

import json
import os
import statistics
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict

# ---------------------------------------------------------------------------
# Storage path (same dir as signals, overridable via env)
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("PREPCAST_DATA_DIR", "/data/prepcast")
SALES_FILE = os.path.join(DATA_DIR, "sales_history.json")


def _load_sales() -> List[Dict]:
    """Load stored sales records from disk."""
    if not os.path.exists(SALES_FILE):
        return []
    try:
        with open(SALES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _day_name(dt: date) -> str:
    return dt.strftime("%A")  # Monday, Tuesday...


def _baseline_by_dow(records: List[Dict]) -> Dict[str, float]:
    """Calculate average daily revenue grouped by day-of-week."""
    buckets: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        try:
            d = date.fromisoformat(r["date"])
            buckets[_day_name(d)].append(float(r["revenue"]))
        except Exception:
            continue
    return {dow: statistics.mean(vals) for dow, vals in buckets.items() if vals}


def _recent_trend(records: List[Dict], days: int = 14) -> Optional[float]:
    """EMA-based trend: returns multiplier vs overall mean (1.0 = flat)."""
    cutoff = date.today() - timedelta(days=days)
    recent = []
    for r in records:
        try:
            if date.fromisoformat(r["date"]) >= cutoff:
                recent.append(float(r["revenue"]))
        except Exception:
            continue
    if not recent or len(records) < 7:
        return None
    all_vals = [float(r["revenue"]) for r in records if "revenue" in r]
    overall_mean = statistics.mean(all_vals)
    recent_mean = statistics.mean(recent)
    return recent_mean / overall_mean if overall_mean else 1.0


# ===========================================================================
# Tool: forecast_sales
# ===========================================================================

FORECAST_SALES_TOOL = {
    "name": "forecast_sales",
    "description": (
        "Forecast projected daily revenue for a given date at this location. "
        "Uses historical sales patterns (day-of-week baseline + recent trend). "
        "Optionally factor in a known event and its expected attendance to "
        "adjust the projection. Returns projected revenue, confidence range, "
        "and a plain-English summary."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target_date": {
                "type": "string",
                "description": "Date to forecast in YYYY-MM-DD format. Defaults to today.",
            },
            "event_name": {
                "type": "string",
                "description": "Optional: name of a known event nearby (e.g. 'volleyball tournament').",
            },
            "event_attendance": {
                "type": "integer",
                "description": "Optional: expected attendance for the event (e.g. 7000).",
            },
            "event_multiplier": {
                "type": "number",
                "description": (
                    "Optional: manually override the event traffic multiplier "
                    "(e.g. 1.4 = 40% more foot traffic). If omitted, estimated from attendance."
                ),
            },
        },
        "required": [],
    },
}


async def handle_forecast_sales(
    target_date: str = "",
    event_name: str = "",
    event_attendance: int = 0,
    event_multiplier: float = 0.0,
) -> str:
    records = _load_sales()

    # Parse target date
    try:
        td = date.fromisoformat(target_date) if target_date else date.today()
    except ValueError:
        return f"Invalid date format: {target_date}. Use YYYY-MM-DD."

    dow = _day_name(td)

    if not records:
        return (
            "No sales history loaded yet. Use upload_report to import your daily sales spreadsheets first."
        )

    baselines = _baseline_by_dow(records)
    base = baselines.get(dow)

    if base is None:
        return (
            f"No historical data for {dow}s yet. Upload more sales reports to build a baseline."
        )

    # Apply recent trend
    trend = _recent_trend(records)
    projected = base * (trend if trend else 1.0)

    # Apply event multiplier
    event_note = ""
    if event_multiplier and event_multiplier > 0:
        projected *= event_multiplier
        event_note = f"Event multiplier applied: {event_multiplier:.2f}x"
    elif event_attendance and event_attendance > 0:
        # Rough heuristic: 1000 attendees ≈ +5% foot traffic up to +60%
        est_mult = min(1.0 + (event_attendance / 1000) * 0.05, 1.60)
        projected *= est_mult
        event_note = (
            f"Event '{event_name}' ({event_attendance:,} attendees) → "
            f"estimated {est_mult:.2f}x multiplier applied."
        )

    # Confidence range ±12% (typical fast-food daily variance)
    all_vals = [float(r["revenue"]) for r in records if "revenue" in r]
    if len(all_vals) >= 10:
        stdev = statistics.stdev(all_vals)
        variance_pct = (stdev / statistics.mean(all_vals)) * 100
    else:
        variance_pct = 12.0

    low = projected * (1 - variance_pct / 100)
    high = projected * (1 + variance_pct / 100)

    lines = [
        f"SALES FORECAST — {td.strftime('%A, %B %d %Y')}",
        f"",
        f"  Projected Revenue:  ${projected:,.0f}",
        f"  Confidence Range:   ${low:,.0f} – ${high:,.0f}  (±{variance_pct:.0f}%)",
        f"  {dow} Baseline:      ${base:,.0f}",
        f"  Recent Trend:       {'%.2fx' % trend if trend else 'not enough data'}",
    ]
    if event_note:
        lines.append(f"  {event_note}")
    lines += [
        f"",
        f"Based on {len(records)} days of sales history.",
    ]
    return "\n".join(lines)


# ===========================================================================
# Tool: analyze_history
# ===========================================================================

ANALYZE_HISTORY_TOOL = {
    "name": "analyze_history",
    "description": (
        "Analyze uploaded sales history to surface patterns: best/worst days, "
        "average revenue by day-of-week, busiest weeks, and overall trend. "
        "Run this after uploading reports to understand your baseline."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "weeks": {
                "type": "integer",
                "description": "How many weeks of history to analyze (default: all).",
            },
        },
        "required": [],
    },
}


async def handle_analyze_history(weeks: int = 0) -> str:
    records = _load_sales()

    if not records:
        return "No sales history found. Use upload_report to import your spreadsheets."

    # Filter by weeks if requested
    if weeks and weeks > 0:
        cutoff = date.today() - timedelta(weeks=weeks)
        records = [r for r in records if date.fromisoformat(r["date"]) >= cutoff]

    if not records:
        return f"No records found in the last {weeks} weeks."

    baselines = _baseline_by_dow(records)
    all_vals = [(date.fromisoformat(r["date"]), float(r["revenue"])) for r in records if "revenue" in r]
    all_vals.sort(key=lambda x: x[0])

    revenues = [v for _, v in all_vals]
    mean_rev = statistics.mean(revenues)
    best_day = max(all_vals, key=lambda x: x[1])
    worst_day = min(all_vals, key=lambda x: x[1])

    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_lines = []
    for dow in dow_order:
        if dow in baselines:
            bar_len = int((baselines[dow] / max(baselines.values())) * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            dow_lines.append(f"  {dow:<10}  {bar}  ${baselines[dow]:,.0f}")

    lines = [
        f"SALES HISTORY ANALYSIS",
        f"  Period:         {all_vals[0][0]} → {all_vals[-1][0]}  ({len(records)} days)",
        f"  Overall Avg:    ${mean_rev:,.0f}/day",
        f"  Best Day:       {best_day[0].strftime('%a %b %d')}  ${best_day[1]:,.0f}",
        f"  Worst Day:      {worst_day[0].strftime('%a %b %d')}  ${worst_day[1]:,.0f}",
        f"",
        f"AVERAGE BY DAY OF WEEK:",
        *dow_lines,
    ]
    return "\n".join(lines)


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [FORECAST_SALES_TOOL, ANALYZE_HISTORY_TOOL]

HANDLERS = {
    "forecast_sales": handle_forecast_sales,
    "analyze_history": handle_analyze_history,
}
