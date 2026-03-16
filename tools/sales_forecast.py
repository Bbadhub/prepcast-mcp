"""
PrepCast - Sales Forecast Tool

Given a location's historical daily sales data, project revenue for a target
date factoring in day-of-week baseline, recent trend (EMA), and event multipliers.
"""

import statistics
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict

from store import load_json

SALES_FILE = "sales_history.json"


def _load_sales(location_id: str = "default") -> List[Dict]:
    return load_json(location_id, SALES_FILE)


def _day_name(dt: date) -> str:
    return dt.strftime("%A")


def _baseline_by_dow(records: List[Dict]) -> Dict[str, float]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        try:
            d = date.fromisoformat(r["date"])
            buckets[_day_name(d)].append(float(r["revenue"]))
        except Exception:
            continue
    return {dow: statistics.mean(vals) for dow, vals in buckets.items() if vals}


def _recent_dow_averages(records: List[Dict], target_date: date, windows: List[int]) -> Dict[int, Optional[float]]:
    """
    Compute recent same-day-of-week averages for the given target date over
    the specified window sizes (in weeks). Returns a mapping:
      window_size_in_weeks -> average_revenue_or_None

    Example: {4: 4800.0, 8: 4950.0}
    """
    results: Dict[int, Optional[float]] = {}
    target_dow = _day_name(target_date)

    # Parse once to avoid repeated conversions
    parsed: List[tuple[date, float]] = []
    for r in records:
        try:
            d = date.fromisoformat(r["date"])
            rev = float(r["revenue"])
            parsed.append((d, rev))
        except Exception:
            continue

    for w in windows:
        cutoff = target_date - timedelta(weeks=w)
        vals = [rev for d, rev in parsed if cutoff <= d < target_date and _day_name(d) == target_dow]
        results[w] = statistics.mean(vals) if vals else None

    return results


def _recency_weighted_forecast(records: List[Dict], target_dow: str, target_date: date) -> Optional[float]:
    """
    Operator-validated forecasting method (per Five Guys manager feedback):
      1. Last week's same day-of-week      -> weight 0.50 (strongest signal)
      2. Same DOW in last 4 weeks          -> weight 0.30
      3. Same DOW in last 13 weeks (qtr)   -> weight 0.20 (baseline anchor)

    Falls back gracefully if history is thin.
    """
    sales_by_date: Dict[str, float] = {}
    for r in records:
        try:
            sales_by_date[r["date"]] = float(r["revenue"])
        except Exception:
            continue

    def same_dow_lookback(weeks_back: int, num_weeks: int) -> List[float]:
        vals = []
        for w in range(weeks_back, weeks_back + num_weeks):
            d = target_date - timedelta(weeks=w)
            v = sales_by_date.get(d.isoformat())
            if v is not None:
                vals.append(v)
        return vals

    last_week = same_dow_lookback(1, 1)       # exactly 7 days ago
    last_4w = same_dow_lookback(1, 4)         # last 4 same-dow days
    last_13w = same_dow_lookback(1, 13)       # last quarter same-dow

    if not last_13w:
        return None  # not enough history

    # Build weighted average
    if last_week and last_4w and len(last_13w) >= 3:
        w1 = statistics.mean(last_week) * 0.50
        w2 = statistics.mean(last_4w) * 0.30
        w3 = statistics.mean(last_13w) * 0.20
        return w1 + w2 + w3
    elif last_4w:
        return statistics.mean(last_4w) * 0.60 + statistics.mean(last_13w) * 0.40
    else:
        return statistics.mean(last_13w)


def _recent_trend(records: List[Dict], days: int = 14) -> Optional[float]:
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


FORECAST_SALES_TOOL = {
    "name": "forecast_sales",
    "description": (
        "Forecast projected daily revenue for a given date. "
        "Uses historical sales patterns (day-of-week baseline + recent EMA trend). "
        "Optionally factor in a local event and its attendance to adjust the projection. "
        "Returns projected revenue, confidence range, and plain-English summary."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target_date": {"type": "string", "description": "Date to forecast YYYY-MM-DD. Defaults to today."},
            "event_name": {"type": "string", "description": "Optional: nearby event name (e.g. 'volleyball tournament')."},
            "event_attendance": {"type": "integer", "description": "Optional: expected event attendance."},
            "event_multiplier": {"type": "number", "description": "Optional: manually override traffic multiplier (e.g. 1.45)."},
        },
        "required": [],
    },
}


async def handle_forecast_sales(arguments: dict) -> str:
    target_date = arguments.get("target_date", "")
    event_name = arguments.get("event_name", "")
    event_attendance = int(arguments.get("event_attendance", 0))
    event_multiplier = float(arguments.get("event_multiplier", 0.0))
    location_id = arguments.get("_location_id", "default")

    records = _load_sales(location_id)

    try:
        td = date.fromisoformat(target_date) if target_date else date.today()
    except ValueError:
        return f"Invalid date format: {target_date}. Use YYYY-MM-DD."

    dow = _day_name(td)

    if not records:
        return (
            "No sales history loaded yet.\n"
            "Use upload_sales_csv to import your daily sales data, "
            "or log_daily_sales to add days manually.\n\n"
            "Tip: Even a few weeks of data gives you a useful baseline."
        )

    # Primary: recency-weighted (last week 50% / last 4 weeks 30% / last quarter 20%)
    projected = _recency_weighted_forecast(records, dow, td)

    # Fallback: DOW average baseline + recent trend multiplier
    baselines = _baseline_by_dow(records)
    # Operator-facing baseline: blend 4-week and 8-week same-DOW averages when available.
    recent_avgs = _recent_dow_averages(records, td, windows=[4, 8])
    avg_4w = recent_avgs.get(4)
    avg_8w = recent_avgs.get(8)

    blended_baseline = None
    if avg_4w is not None and avg_8w is not None:
        # Bias toward 8 weeks per GM preference.
        blended_baseline = avg_4w * 0.30 + avg_8w * 0.70
    elif avg_8w is not None:
        blended_baseline = avg_8w
    elif avg_4w is not None:
        blended_baseline = avg_4w

    # Fallback: historical all-time DOW average + recent trend if recent windows are thin.
    base_dow_all_time = baselines.get(dow)
    if blended_baseline is None:
        if base_dow_all_time is None:
            return f"No historical data for {dow}s yet. Upload more sales reports to build a baseline."
        trend = _recent_trend(records)
        blended_baseline = base_dow_all_time * (trend if trend else 1.0)

    # Use blended baseline as primary projection unless recency-weighted method exists and you prefer it.
    projected = blended_baseline

    event_note = ""
    if event_multiplier and event_multiplier > 0:
        projected *= event_multiplier
        event_note = f"Event multiplier applied: {event_multiplier:.2f}x"
    elif event_attendance and event_attendance > 0:
        est_mult = min(1.0 + (event_attendance / 1000) * 0.05, 1.60)
        projected *= est_mult
        event_note = f"Event '{event_name}' ({event_attendance:,} attendees) -> {est_mult:.2f}x multiplier"

    all_vals = [float(r["revenue"]) for r in records if "revenue" in r]
    if len(all_vals) >= 10:
        stdev = statistics.stdev(all_vals)
        variance_pct = (stdev / statistics.mean(all_vals)) * 100
    else:
        variance_pct = 12.0

    low = projected * (1 - variance_pct / 100)
    high = projected * (1 + variance_pct / 100)

    lines = [
        f"SALES FORECAST - {td.strftime('%A, %B %d %Y')}",
        f"",
    ]

    # Baseline breakdown: 4-week vs 8-week vs blended.
    lines.append("  BASELINE (same day-of-week):")
    if avg_4w is not None:
        lines.append(f"    4-week average:      ${avg_4w:,.0f}")
    else:
        lines.append(f"    4-week average:      n/a (not enough history)")
    if avg_8w is not None:
        lines.append(f"    8-week average:      ${avg_8w:,.0f}")
    else:
        lines.append(f"    8-week average:      n/a (not enough history)")

    lines.append(f"    Blended baseline:    ${blended_baseline:,.0f}  (30% 4w / 70% 8w when both are available)")
    if base_dow_all_time is not None:
        lines.append(f"    All-time {dow} avg:   ${base_dow_all_time:,.0f}  (for reference)")

    lines += [
        f"",
        f"  ADJUSTMENTS:",
    ]
    if event_note:
        lines.append(f"    Event:               {event_note}")
    else:
        lines.append(f"    Event:               None (no event multiplier applied)")

    lines += [
        f"",
        f"  FINAL FORECAST:",
        f"    Projected Revenue:   ${projected:,.0f}",
        f"    Confidence Range:    ${low:,.0f} - ${high:,.0f}  (+/-{variance_pct:.0f}%)",
        f"",
        f"Based on {len(records)} days of sales history.",
        f"Run generate_prep_list with projected_revenue={projected:.0f} to get your prep list.",
    ]
    return "\n".join(lines)


ANALYZE_HISTORY_TOOL = {
    "name": "analyze_history",
    "description": (
        "Analyze uploaded sales history to surface patterns: best/worst days, "
        "average revenue by day-of-week, and overall trend. "
        "Run this after uploading reports to understand your baseline."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "weeks": {"type": "integer", "description": "How many weeks of history to analyze (default: all)."},
        },
        "required": [],
    },
}


async def handle_analyze_history(arguments: dict) -> str:
    weeks = int(arguments.get("weeks", 0))
    location_id = arguments.get("_location_id", "default")
    records = _load_sales(location_id)

    if not records:
        return "No sales history found. Use upload_sales_csv to import your spreadsheets."

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
            bar = "#" * bar_len + "." * (20 - bar_len)
            dow_lines.append(f"  {dow:<10}  {bar}  ${baselines[dow]:,.0f}")

    lines = [
        f"SALES HISTORY ANALYSIS",
        f"  Period:         {all_vals[0][0]} -> {all_vals[-1][0]}  ({len(records)} days)",
        f"  Overall Avg:    ${mean_rev:,.0f}/day",
        f"  Best Day:       {best_day[0].strftime('%a %b %d')}  ${best_day[1]:,.0f}",
        f"  Worst Day:      {worst_day[0].strftime('%a %b %d')}  ${worst_day[1]:,.0f}",
        f"",
        f"AVERAGE BY DAY OF WEEK:",
        *dow_lines,
    ]
    return "\n".join(lines)


TOOLS = [FORECAST_SALES_TOOL, ANALYZE_HISTORY_TOOL]

HANDLERS = {
    "forecast_sales": handle_forecast_sales,
    "analyze_history": handle_analyze_history,
}
