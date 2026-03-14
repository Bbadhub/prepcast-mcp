"""
PrepCast - KPI Reporting Tools (MCP)

These tools let Claude answer questions like:
  "How are my forecasts doing?"
  "Which day of week drives the most revenue?"
  "Show me event impact"

Designed to produce rich text outputs that Claude renders as artifacts.

Tools:
  get_performance_report   - full KPI summary for your store
  get_forecast_accuracy    - prediction vs actual breakdown
  get_revenue_trends       - weekly/monthly trend data with narrative
"""

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from store import (
    load_json,
    load_json_dict,
    get_location_name,
    DATA_DIR,
)

FORECAST_LOG_FILE = "forecast_log.json"
SALES_FILE = "sales_history.json"
EVENT_OUTCOMES_FILE = "event_outcomes.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sales_stats(records: List[Dict], days: int = None) -> Dict:
    """Aggregate sales records into a stats dict."""
    if days:
        cutoff = date.today() - timedelta(days=days)
        records = [r for r in records if _parse_date(r.get("date", "")) >= cutoff]

    revenues = []
    by_dow: Dict[str, List[float]] = defaultdict(list)
    by_week: Dict[str, float] = {}
    by_month: Dict[str, float] = {}

    for r in records:
        try:
            d = _parse_date(r["date"])
            rev = float(r["revenue"])
            revenues.append(rev)
            by_dow[d.strftime("%A")].append(rev)
            by_week[d.strftime("%Y-W%W")] = by_week.get(d.strftime("%Y-W%W"), 0) + rev
            by_month[d.strftime("%Y-%m")] = by_month.get(d.strftime("%Y-%m"), 0) + rev
        except Exception:
            continue

    if not revenues:
        return {"empty": True}

    sorted_recs = sorted(records, key=lambda r: r.get("date", ""))
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    return {
        "empty": False,
        "days": len(revenues),
        "date_from": sorted_recs[0]["date"] if sorted_recs else None,
        "date_to": sorted_recs[-1]["date"] if sorted_recs else None,
        "avg_daily": round(statistics.mean(revenues), 0),
        "median_daily": round(statistics.median(revenues), 0),
        "best": round(max(revenues), 0),
        "worst": round(min(revenues), 0),
        "total": round(sum(revenues), 0),
        "stdev": round(statistics.stdev(revenues), 0) if len(revenues) >= 2 else 0,
        "by_dow": {
            dow: round(statistics.mean(by_dow[dow]), 0)
            for dow in dow_order if dow in by_dow
        },
        "by_week": dict(sorted(by_week.items())[-12:]),
        "by_month": dict(sorted(by_month.items())[-6:]),
    }


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _forecast_accuracy(sales_records: List[Dict], forecast_records: List[Dict]) -> Dict:
    sales_by_date = {r["date"]: float(r["revenue"]) for r in sales_records if "revenue" in r}
    comparisons = []
    for f in forecast_records:
        td = f.get("target_date", "")
        if td in sales_by_date:
            projected = float(f.get("projected_revenue", 0))
            actual = sales_by_date[td]
            if projected > 0:
                err = abs(actual - projected) / projected * 100
                comparisons.append({
                    "date": td,
                    "projected": round(projected, 0),
                    "actual": round(actual, 0),
                    "error_pct": round(err, 1),
                    "direction": "over" if actual > projected else "under",
                })
    if not comparisons:
        return {"available": 0}
    errors = [c["error_pct"] for c in comparisons]
    return {
        "available": len(comparisons),
        "avg_error_pct": round(statistics.mean(errors), 1),
        "median_error_pct": round(statistics.median(errors), 1),
        "within_5pct": sum(1 for e in errors if e <= 5),
        "within_10pct": sum(1 for e in errors if e <= 10),
        "within_15pct": sum(1 for e in errors if e <= 15),
        "over_15pct": sum(1 for e in errors if e > 15),
        "accuracy_pct": round(100 - statistics.mean(errors), 1),
        "recent": sorted(comparisons, key=lambda x: x["date"], reverse=True)[:10],
    }


def _trend_sentence(stats: Dict, prior_stats: Dict) -> str:
    if stats.get("empty") or prior_stats.get("empty"):
        return "Not enough data to compare periods."
    curr = stats["avg_daily"]
    prev = prior_stats["avg_daily"]
    diff = curr - prev
    pct = diff / prev * 100 if prev else 0
    dir_word = "up" if diff >= 0 else "down"
    return (
        f"Average daily revenue is {dir_word} {abs(pct):.1f}% "
        f"(${abs(diff):,.0f}/day) vs the prior 30-day period."
    )


def _location_summary(location_id: str) -> str:
    """One-location full text report."""
    name = get_location_name(location_id)
    sales = load_json(location_id, SALES_FILE)
    forecast_log = load_json(location_id, FORECAST_LOG_FILE)
    events = load_json(location_id, EVENT_OUTCOMES_FILE)

    stats = _sales_stats(sales)
    stats_30 = _sales_stats(sales, days=30)
    stats_prev30 = _sales_stats(
        [r for r in sales
         if _parse_date_safe(r.get("date", "")) is not None
         and date.today() - timedelta(days=60) <= _parse_date_safe(r["date"]) < date.today() - timedelta(days=30)],
    )
    accuracy = _forecast_accuracy(sales, forecast_log)

    if stats.get("empty"):
        return f"LOCATION: {name}\n\n  No sales data on file yet.\n  Use upload_sales_csv or log_daily_sales to add data."

    # Day of week best/worst
    dow = stats.get("by_dow", {})
    if dow:
        best_dow = max(dow, key=dow.get)
        worst_dow = min(dow, key=dow.get)
    else:
        best_dow = worst_dow = "N/A"

    # Event summary
    event_summary = ""
    if events:
        lifts = []
        for e in events:
            baseline = float(e.get("baseline_revenue", 0))
            actual = float(e.get("actual_revenue", 0))
            if baseline > 0:
                lifts.append(actual - baseline)
        if lifts:
            avg_lift = statistics.mean(lifts)
            event_summary = f"\n  Events logged:     {len(events)} ({len(lifts)} with lift data, avg +${avg_lift:,.0f}/event)"

    trend = _trend_sentence(stats_30, stats_prev30)

    lines = [
        f"PERFORMANCE REPORT: {name.upper()}",
        f"{'=' * 52}",
        f"",
        f"  Period:            {stats['date_from']} to {stats['date_to']}",
        f"  Days on record:    {stats['days']}",
        f"",
        f"REVENUE",
        f"  Overall avg/day:   ${stats['avg_daily']:,.0f}",
        f"  Last 30d avg/day:  ${stats_30.get('avg_daily', 0):,.0f}",
        f"  Median/day:        ${stats['median_daily']:,.0f}",
        f"  Best day:          ${stats['best']:,.0f}",
        f"  Worst day:         ${stats['worst']:,.0f}",
        f"  Trend:             {trend}",
        f"",
        f"DAY OF WEEK (avg revenue)",
    ]
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    max_dow = max(dow.values()) if dow else 1
    for d in dow_order:
        if d in dow:
            bar = "#" * int(dow[d] / max_dow * 20)
            lines.append(f"  {d:<12} {bar:<20} ${dow[d]:,.0f}")
    lines += [
        f"  Best day:          {best_dow} (${dow.get(best_dow, 0):,.0f} avg)",
        f"  Worst day:         {worst_dow} (${dow.get(worst_dow, 0):,.0f} avg)",
        f"",
        f"FORECAST ACCURACY",
    ]
    if accuracy["available"] == 0:
        lines.append("  No forecast comparisons available yet.")
        lines.append("  (Forecasts are compared to actuals after you log daily sales.)")
    else:
        lines += [
            f"  Comparisons:       {accuracy['available']}",
            f"  Accuracy:          {accuracy['accuracy_pct']}%  (avg error: {accuracy['avg_error_pct']}%)",
            f"  Within 5%:         {accuracy['within_5pct']} forecasts",
            f"  Within 10%:        {accuracy['within_10pct']} forecasts",
            f"  Over 15% off:      {accuracy['over_15pct']} forecasts",
        ]
        if accuracy["recent"]:
            lines += ["", "  Recent comparisons:"]
            lines.append(f"  {'Date':<12} {'Forecast':>10} {'Actual':>10} {'Error':>8} {'Dir':<6}")
            lines.append(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*6}")
            for c in accuracy["recent"][:5]:
                lines.append(f"  {c['date']:<12} ${c['projected']:>9,.0f} ${c['actual']:>9,.0f} {c['error_pct']:>7.1f}% {c['direction']:<6}")

    if event_summary:
        lines += ["", f"EVENTS{event_summary}"]

    # Monthly revenue table
    by_month = stats.get("by_month", {})
    if by_month:
        lines += ["", "MONTHLY REVENUE"]
        for month, total in sorted(by_month.items()):
            lines.append(f"  {month}    ${total:,.0f}")

    return "\n".join(lines)


def _parse_date_safe(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool: get_performance_report
# ---------------------------------------------------------------------------

GET_PERFORMANCE_REPORT_TOOL = {
    "name": "get_performance_report",
    "description": (
        "Get a full KPI performance report for your store. "
        "Shows revenue averages, day-of-week breakdown, forecast accuracy, event impact, "
        "and monthly trends. Great for generating Claude artifacts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle_get_performance_report(arguments: dict) -> str:
    location_id = arguments.get("_location_id", "default")
    return _location_summary(location_id)


# ---------------------------------------------------------------------------
# Tool: get_forecast_accuracy
# ---------------------------------------------------------------------------

GET_FORECAST_ACCURACY_TOOL = {
    "name": "get_forecast_accuracy",
    "description": (
        "Show a detailed breakdown of how accurate PrepCast's revenue forecasts have been "
        "vs actual daily sales. Includes per-forecast comparison, error distribution, "
        "and whether the model tends to over- or under-predict."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "location_id": {
                "type": "string",
                "description": "Location to analyze. Leave blank for your own store.",
            },
            "weeks": {
                "type": "integer",
                "description": "How many weeks back to include (default: all time).",
            },
        },
        "required": [],
    },
}


async def handle_get_forecast_accuracy(arguments: dict) -> str:
    location_id = (arguments.get("location_id") or "default").strip()
    weeks = int(arguments.get("weeks") or 0)
    name = get_location_name(location_id)

    sales = load_json(location_id, SALES_FILE)
    forecast_log = load_json(location_id, FORECAST_LOG_FILE)

    if weeks > 0:
        cutoff = date.today() - timedelta(weeks=weeks)
        sales = [r for r in sales if _parse_date_safe(r.get("date", "")) and _parse_date_safe(r["date"]) >= cutoff]
        forecast_log = [f for f in forecast_log if _parse_date_safe(f.get("target_date", "")) and _parse_date_safe(f["target_date"]) >= cutoff]

    accuracy = _forecast_accuracy(sales, forecast_log)

    if accuracy.get("available", 0) == 0:
        return (
            f"FORECAST ACCURACY: {name}\n\n"
            "No forecast-vs-actual comparisons available yet.\n\n"
            "How this works:\n"
            "  1. Run forecast_sales for a future date\n"
            "  2. At end of day, run log_daily_sales with actual revenue\n"
            "  3. PrepCast compares predicted vs actual automatically\n\n"
            "The more comparisons you log, the better your accuracy score gets."
        )

    # Over/under distribution
    recent = accuracy.get("recent", [])
    over_count = sum(1 for c in recent if c["direction"] == "over")
    under_count = sum(1 for c in recent if c["direction"] == "under")

    lines = [
        f"FORECAST ACCURACY REPORT: {name.upper()}",
        "=" * 52,
        "",
        f"  Comparisons logged:   {accuracy['available']}",
        f"  Overall accuracy:     {accuracy['accuracy_pct']}%",
        f"  Avg error:            {accuracy['avg_error_pct']}%",
        f"  Median error:         {accuracy['median_error_pct']}%",
        "",
        "ACCURACY DISTRIBUTION",
        f"  Within  5%:  {accuracy['within_5pct']:>3} forecasts  {'#' * accuracy['within_5pct']}",
        f"  Within 10%:  {accuracy['within_10pct']:>3} forecasts  {'#' * accuracy['within_10pct']}",
        f"  Within 15%:  {accuracy['within_15pct']:>3} forecasts  {'#' * accuracy['within_15pct']}",
        f"  Over  15%:   {accuracy['over_15pct']:>3} forecasts  {'#' * accuracy['over_15pct']}",
        "",
        "OVER/UNDER BIAS",
        f"  Actual > Forecast (you prepped light): {over_count}x",
        f"  Actual < Forecast (you prepped heavy): {under_count}x",
    ]
    if over_count > under_count * 1.5:
        lines.append("  -> Model tends to under-predict. Consider adding a small buffer.")
    elif under_count > over_count * 1.5:
        lines.append("  -> Model tends to over-predict. You may be over-prepping slightly.")
    else:
        lines.append("  -> Bias is balanced. Good calibration.")

    lines += ["", "RECENT FORECASTS", f"  {'Date':<12} {'Forecast':>10} {'Actual':>10} {'Error':>8} {'Direction':<10}",
              f"  {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*10}"]
    for c in accuracy.get("recent", []):
        lines.append(
            f"  {c['date']:<12} ${c['projected']:>9,.0f} ${c['actual']:>9,.0f} "
            f"{c['error_pct']:>7.1f}% {c['direction']:<10}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: get_revenue_trends
# ---------------------------------------------------------------------------

GET_REVENUE_TRENDS_TOOL = {
    "name": "get_revenue_trends",
    "description": (
        "Show revenue trends over time: weekly totals, monthly averages, "
        "day-of-week patterns, and a plain-English narrative summary. "
        "Use this when the manager wants to understand if business is growing, "
        "which days are strongest, or how this month compares to last."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "location_id": {
                "type": "string",
                "description": "Location to analyze. Leave blank for your own store.",
            },
            "period": {
                "type": "string",
                "description": "Time period: '30d', '90d', '6m', '1y', or 'all'. Default: all.",
            },
        },
        "required": [],
    },
}


async def handle_get_revenue_trends(arguments: dict) -> str:
    location_id = (arguments.get("location_id") or "default").strip()
    period = (arguments.get("period") or "all").lower().strip()
    name = get_location_name(location_id)

    sales = load_json(location_id, SALES_FILE)
    if not sales:
        return (
            f"REVENUE TRENDS: {name}\n\n"
            "No sales data on file yet.\n"
            "Use upload_sales_csv to import your daily sales spreadsheets."
        )

    # Apply period filter
    period_map = {"30d": 30, "90d": 90, "6m": 180, "1y": 365}
    if period in period_map:
        cutoff = date.today() - timedelta(days=period_map[period])
        filtered = [r for r in sales if _parse_date_safe(r.get("date", "")) and _parse_date_safe(r["date"]) >= cutoff]
    else:
        filtered = sales

    stats = _sales_stats(filtered)
    stats_30 = _sales_stats(filtered, days=30)

    prev30 = [
        r for r in filtered
        if _parse_date_safe(r.get("date", "")) is not None
        and date.today() - timedelta(days=60) <= _parse_date_safe(r["date"]) < date.today() - timedelta(days=30)
    ]
    stats_prev30 = _sales_stats(prev30)

    if stats.get("empty"):
        return f"REVENUE TRENDS: {name}\n\nNo data in the selected period ({period})."

    trend = _trend_sentence(stats_30, stats_prev30)

    dow = stats.get("by_dow", {})
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    max_dow = max(dow.values()) if dow else 1
    best_dow = max(dow, key=dow.get) if dow else "N/A"
    worst_dow = min(dow, key=dow.get) if dow else "N/A"

    lines = [
        f"REVENUE TRENDS: {name.upper()}",
        "=" * 52,
        f"Period: {stats['date_from']} to {stats['date_to']}  ({stats['days']} days)",
        "",
        "SUMMARY",
        f"  {trend}",
        f"  Overall avg:   ${stats['avg_daily']:,.0f}/day",
        f"  Last 30d avg:  ${stats_30.get('avg_daily', 0):,.0f}/day",
        f"  Best day ever: ${stats['best']:,.0f}",
        f"  Variability:   +/-${stats['stdev']:,.0f}/day (1 std dev)",
        "",
        "DAY OF WEEK AVERAGES",
    ]
    for d in dow_order:
        if d in dow:
            bar = "#" * int(dow[d] / max_dow * 24)
            pct = dow[d] / stats["avg_daily"] * 100
            lines.append(f"  {d:<10}  {bar:<24}  ${dow[d]:,.0f}  ({pct:.0f}% of avg)")

    lines += [
        f"",
        f"  Strongest day: {best_dow} (${dow.get(best_dow, 0):,.0f} avg)",
        f"  Lightest day:  {worst_dow} (${dow.get(worst_dow, 0):,.0f} avg)",
        f"  Weekend premium: " + (
            f"+{((dow.get('Saturday', 0) + dow.get('Sunday', 0)) / 2 - stats['avg_daily']) / stats['avg_daily'] * 100:.0f}%"
            if "Saturday" in dow and "Sunday" in dow else "N/A"
        ),
        "",
        "WEEKLY REVENUE (last 12 weeks)",
    ]
    by_week = stats.get("by_week", {})
    max_week = max(by_week.values()) if by_week else 1
    for week, total in sorted(by_week.items()):
        bar = "#" * int(total / max_week * 24)
        lines.append(f"  {week}  {bar:<24}  ${total:,.0f}")

    lines += ["", "MONTHLY REVENUE"]
    by_month = stats.get("by_month", {})
    prev_total = None
    for month, total in sorted(by_month.items()):
        change = ""
        if prev_total:
            pct = (total - prev_total) / prev_total * 100
            change = f"  ({'+' if pct >= 0 else ''}{pct:.1f}% vs prior)"
        lines.append(f"  {month}    ${total:,.0f}{change}")
        prev_total = total

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOLS = [
    GET_PERFORMANCE_REPORT_TOOL,
    GET_FORECAST_ACCURACY_TOOL,
    GET_REVENUE_TRENDS_TOOL,
]

HANDLERS = {
    "get_performance_report": handle_get_performance_report,
    "get_forecast_accuracy": handle_get_forecast_accuracy,
    "get_revenue_trends": handle_get_revenue_trends,
}
