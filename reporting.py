"""
PrepCast - KPI Reporting

Aggregates forecast accuracy, sales trends, and prep efficiency
for the owner dashboard. Reads from sales_history.json and
event_outcomes.json to produce prediction-vs-actual comparisons.
"""

import json
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import web

DATA_DIR = Path(os.environ.get("PREPCAST_DATA_DIR", "/tmp/prepcast"))
SALES_FILE = DATA_DIR / "sales_history.json"
EVENT_OUTCOMES_FILE = DATA_DIR / "event_outcomes.json"
FORECAST_LOG_FILE = DATA_DIR / "forecast_log.json"


def _load(path: Path) -> List:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _load_dict(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _log_forecast(projected: float, target_date: str, context: str = ""):
    """Append a forecast record so we can compare against actuals later."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = _load(FORECAST_LOG_FILE)
    if not isinstance(log, list):
        log = []
    log.append({
        "forecast_date": datetime.utcnow().isoformat(),
        "target_date": target_date,
        "projected_revenue": projected,
        "context": context,
    })
    FORECAST_LOG_FILE.write_text(json.dumps(log, indent=2))


def _get_kpi_summary() -> Dict:
    sales = _load(SALES_FILE)
    if not isinstance(sales, list):
        sales = []

    outcomes = _load(EVENT_OUTCOMES_FILE)
    if not isinstance(outcomes, list):
        outcomes = []

    forecast_log = _load(FORECAST_LOG_FILE)
    if not isinstance(forecast_log, list):
        forecast_log = []

    # --- Revenue trend (last 30 days vs prior 30 days) ---
    today = date.today()
    cutoff_30 = today - timedelta(days=30)
    cutoff_60 = today - timedelta(days=60)

    recent_revenues = []
    prior_revenues = []
    by_dow: Dict[str, List[float]] = defaultdict(list)
    by_week: Dict[str, float] = {}

    for r in sales:
        try:
            d = date.fromisoformat(r["date"])
            rev = float(r["revenue"])
            dow = d.strftime("%A")
            by_dow[dow].append(rev)
            week_key = d.strftime("%Y-W%W")
            by_week[week_key] = by_week.get(week_key, 0) + rev
            if d >= cutoff_30:
                recent_revenues.append(rev)
            elif d >= cutoff_60:
                prior_revenues.append(rev)
        except Exception:
            continue

    recent_avg = statistics.mean(recent_revenues) if recent_revenues else 0
    prior_avg = statistics.mean(prior_revenues) if prior_revenues else 0
    trend_pct = ((recent_avg - prior_avg) / prior_avg * 100) if prior_avg else 0

    # --- Forecast accuracy ---
    accuracy_records = []
    forecast_by_date = {f["target_date"]: f for f in forecast_log}
    sales_by_date = {r["date"]: float(r["revenue"]) for r in sales}

    for target_date, forecast in forecast_by_date.items():
        if target_date in sales_by_date:
            projected = forecast["projected_revenue"]
            actual = sales_by_date[target_date]
            if projected > 0:
                error_pct = abs(actual - projected) / projected * 100
                accuracy_records.append({
                    "date": target_date,
                    "projected": round(projected, 0),
                    "actual": round(actual, 0),
                    "error_pct": round(error_pct, 1),
                    "over_under": "over" if actual > projected else "under",
                })

    avg_error_pct = (
        statistics.mean(r["error_pct"] for r in accuracy_records)
        if accuracy_records else None
    )
    forecast_accuracy_pct = (100 - avg_error_pct) if avg_error_pct is not None else None

    # --- Event impact analysis ---
    event_impact = []
    for o in outcomes:
        baseline = float(o.get("baseline_revenue", 0))
        actual = float(o.get("actual_revenue", 0))
        if baseline > 0:
            actual_mult = round(actual / baseline, 3)
            event_impact.append({
                "date": o.get("event_date", ""),
                "event": o.get("event_name", ""),
                "type": o.get("event_type", ""),
                "attendance": o.get("attendance", 0),
                "baseline": round(baseline, 0),
                "actual": round(actual, 0),
                "multiplier": actual_mult,
                "revenue_lift": round(actual - baseline, 0),
            })

    # --- Best/worst performers ---
    all_revenues = [float(r["revenue"]) for r in sales if "revenue" in r]
    all_dates = sorted(
        [(r["date"], float(r["revenue"])) for r in sales if "revenue" in r],
        key=lambda x: x[0]
    )

    # Weekly totals for chart
    weekly_data = sorted(
        [{"week": k, "revenue": round(v, 0)} for k, v in by_week.items()],
        key=lambda x: x["week"]
    )[-12:]  # last 12 weeks

    # Day-of-week averages
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_averages = []
    for dow in dow_order:
        if dow in by_dow:
            vals = by_dow[dow]
            dow_averages.append({
                "day": dow,
                "avg_revenue": round(statistics.mean(vals), 0),
                "samples": len(vals),
            })

    return {
        "summary": {
            "total_days_recorded": len(sales),
            "date_range": {
                "from": all_dates[0][0] if all_dates else None,
                "to": all_dates[-1][0] if all_dates else None,
            },
            "overall_avg_daily_revenue": round(statistics.mean(all_revenues), 0) if all_revenues else 0,
            "recent_30d_avg": round(recent_avg, 0),
            "prior_30d_avg": round(prior_avg, 0),
            "trend_pct": round(trend_pct, 1),
            "best_day_revenue": round(max(all_revenues), 0) if all_revenues else 0,
            "worst_day_revenue": round(min(all_revenues), 0) if all_revenues else 0,
        },
        "forecast_accuracy": {
            "comparisons_available": len(accuracy_records),
            "avg_error_pct": round(avg_error_pct, 1) if avg_error_pct is not None else None,
            "accuracy_pct": round(forecast_accuracy_pct, 1) if forecast_accuracy_pct is not None else None,
            "recent": sorted(accuracy_records, key=lambda x: x["date"], reverse=True)[:10],
        },
        "event_impact": {
            "total_events_logged": len(event_impact),
            "events": sorted(event_impact, key=lambda x: x["date"], reverse=True)[:10],
        },
        "charts": {
            "weekly_revenue": weekly_data,
            "by_day_of_week": dow_averages,
        },
    }


def add_reporting_routes(app: web.Application):
    """Register /api/* reporting routes."""

    async def handle_kpi(request: web.Request) -> web.Response:
        """GET /api/kpi - Full KPI dashboard data."""
        data = _get_kpi_summary()
        return web.json_response(data)

    async def handle_kpi_summary(request: web.Request) -> web.Response:
        """GET /api/kpi/summary - Quick numbers only."""
        data = _get_kpi_summary()
        return web.json_response(data["summary"])

    async def handle_kpi_accuracy(request: web.Request) -> web.Response:
        """GET /api/kpi/accuracy - Forecast vs actuals."""
        data = _get_kpi_summary()
        return web.json_response(data["forecast_accuracy"])

    async def handle_kpi_events(request: web.Request) -> web.Response:
        """GET /api/kpi/events - Event impact records."""
        data = _get_kpi_summary()
        return web.json_response(data["event_impact"])

    async def handle_kpi_charts(request: web.Request) -> web.Response:
        """GET /api/kpi/charts - Chart data (weekly revenue, DoW averages)."""
        data = _get_kpi_summary()
        return web.json_response(data["charts"])

    app.router.add_get("/api/kpi", handle_kpi)
    app.router.add_get("/api/kpi/summary", handle_kpi_summary)
    app.router.add_get("/api/kpi/accuracy", handle_kpi_accuracy)
    app.router.add_get("/api/kpi/events", handle_kpi_events)
    app.router.add_get("/api/kpi/charts", handle_kpi_charts)
