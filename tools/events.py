"""
PrepCast - Events Tool

Manages event-based traffic multipliers for locations near sports venues.
Supports AdventHealth Sports Park (Blue Hawk) in Overland Park KS by default.
"""

import re
import urllib.request
import urllib.error
from datetime import date, datetime
from typing import Dict, List, Optional
import statistics

from store import load_json, save_json

EVENT_OUTCOMES_FILE = "event_outcomes.json"

DEFAULT_EVENT_MULTIPLIERS = {
    "volleyball": 1.45,
    "soccer": 1.35,
    "football": 1.30,
    "baseball": 1.25,
    "softball": 1.25,
    "basketball": 1.20,
    "hockey": 1.20,
    "cornhole": 1.05,
    "cheerleading": 1.40,
    "wrestling": 1.30,
    "gymnastics": 1.25,
    "swim": 1.15,
    "track": 1.10,
    "lacrosse": 1.20,
    "default": 1.15,
}


def attendance_to_multiplier(attendance: int) -> float:
    return min(1.0 + (attendance / 1000) * 0.05, 1.60)


def _classify_event(title: str) -> str:
    title_lower = title.lower()
    for sport in DEFAULT_EVENT_MULTIPLIERS:
        if sport in title_lower:
            return sport
    return "default"


def _load_outcomes(location_id: str = "default") -> List[Dict]:
    return load_json(location_id, EVENT_OUTCOMES_FILE)


def _save_outcomes(outcomes: List[Dict], location_id: str = "default"):
    save_json(location_id, EVENT_OUTCOMES_FILE, outcomes)


def _learned_multipliers(location_id: str = "default") -> Dict[str, float]:
    outcomes = _load_outcomes(location_id)
    if not outcomes:
        return {}
    buckets: Dict[str, List[float]] = {}
    for o in outcomes:
        etype = o.get("event_type", "default")
        baseline = o.get("baseline_revenue", 0)
        actual = o.get("actual_revenue", 0)
        if baseline and actual:
            mult = actual / baseline
            buckets.setdefault(etype, []).append(mult)
    return {etype: round(statistics.mean(vals), 3) for etype, vals in buckets.items() if vals}


GET_UPCOMING_EVENTS_TOOL = {
    "name": "get_upcoming_events",
    "description": (
        "Show upcoming events at AdventHealth Sports Park (Blue Hawk) "
        "in Overland Park KS, with estimated foot traffic multipliers. "
        "Use before forecasting sales on a busy weekend."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "days_ahead": {"type": "integer", "description": "Days ahead to look (default: 14)."},
        },
        "required": [],
    },
}


async def handle_get_upcoming_events(arguments: dict) -> str:
    days_ahead = int(arguments.get("days_ahead", 14))
    location_id = arguments.get("_location_id", "default")
    learned = _learned_multipliers(location_id)
    events_found = []
    fetch_note = ""

    urls_to_try = [
        "https://www.adventhealthsportspark.com/events",
        "https://www.bluehawksportspark.com/events",
    ]

    raw_html = ""
    for url in urls_to_try:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 PrepCast/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_html = resp.read().decode("utf-8", errors="ignore")
                fetch_note = f"Fetched from {url}"
                break
        except Exception as e:
            fetch_note = f"Could not fetch live events ({e})"

    if raw_html:
        title_matches = re.findall(
            r'(?:class="[^"]*(?:event|title)[^"]*"[^>]*>|<h[123][^>]*>)\s*([A-Za-z][^<]{5,80})</(?:h[123]|[a-z])',
            raw_html, re.IGNORECASE,
        )
        date_matches = re.findall(
            r'(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b)',
            raw_html,
        )
        seen = set()
        for i, title in enumerate(title_matches[:20]):
            title = title.strip()
            if title and title not in seen and len(title) > 8:
                seen.add(title)
                etype = _classify_event(title)
                mult = learned.get(etype, DEFAULT_EVENT_MULTIPLIERS.get(etype, DEFAULT_EVENT_MULTIPLIERS["default"]))
                event_date = date_matches[i] if i < len(date_matches) else "date TBD"
                events_found.append({"title": title, "date": event_date, "type": etype, "multiplier": mult})

    if events_found:
        lines = [f"UPCOMING EVENTS - AdventHealth Sports Park", f"  {fetch_note}", f""]
        for e in events_found[:10]:
            src = "learned" if e["type"] in learned else "estimated"
            lines.append(f"  {e['date']:<20}  {e['title'][:40]:<42}  {e['multiplier']:.2f}x  [{src}]")
        lines += ["", "Use these multipliers in forecast_sales -> event_multiplier param."]
        return "\n".join(lines)

    lines = [
        f"ADVENTHEALTH SPORTS PARK - EVENT LOOKUP",
        f"  {fetch_note}",
        f"",
        f"  Check manually: https://www.adventhealthsportspark.com/events",
        f"  Then run: forecast_sales with event_name + event_attendance params",
        f"",
        f"FOOT TRAFFIC MULTIPLIERS BY EVENT TYPE:",
    ]
    all_mults = {**DEFAULT_EVENT_MULTIPLIERS}
    all_mults.update(learned)
    for etype, mult in sorted(all_mults.items(), key=lambda x: -x[1]):
        src = " (learned)" if etype in learned else ""
        lines.append(f"  {etype:<20}  {mult:.2f}x{src}")
    return "\n".join(lines)


LOG_EVENT_OUTCOME_TOOL = {
    "name": "log_event_outcome",
    "description": (
        "Log actual sales for an event day. Teaches the system how each event type "
        "affects your specific location's foot traffic over time."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "event_date": {"type": "string", "description": "Date of the event YYYY-MM-DD."},
            "event_name": {"type": "string", "description": "Event name."},
            "event_type": {"type": "string", "description": "Type: volleyball, soccer, cornhole, etc."},
            "attendance": {"type": "integer", "description": "Actual or estimated attendance."},
            "actual_revenue": {"type": "number", "description": "What you actually rang up."},
            "baseline_revenue": {"type": "number", "description": "Normal same-day-of-week revenue."},
        },
        "required": ["event_date", "event_name", "actual_revenue", "baseline_revenue"],
    },
}


async def handle_log_event_outcome(arguments: dict) -> str:
    event_date = arguments.get("event_date", "")
    event_name = arguments.get("event_name", "")
    event_type = arguments.get("event_type", "")
    attendance = int(arguments.get("attendance", 0))
    actual_revenue = float(arguments.get("actual_revenue", 0))
    baseline_revenue = float(arguments.get("baseline_revenue", 0))

    if not event_date or not event_name or not actual_revenue or not baseline_revenue:
        return "event_date, event_name, actual_revenue, and baseline_revenue are all required."

    etype = event_type or _classify_event(event_name)
    multiplier = round(actual_revenue / baseline_revenue, 3) if baseline_revenue else 0

    location_id = arguments.get("_location_id", "default")
    outcomes = _load_outcomes(location_id)
    outcomes.append({
        "event_date": event_date,
        "event_name": event_name,
        "event_type": etype,
        "attendance": attendance,
        "actual_revenue": actual_revenue,
        "baseline_revenue": baseline_revenue,
        "actual_multiplier": multiplier,
        "logged_at": datetime.utcnow().isoformat(),
    })
    _save_outcomes(outcomes, location_id)

    learned = _learned_multipliers(location_id)
    new_mult = learned.get(etype, multiplier)
    count = sum(1 for o in outcomes if o.get("event_type") == etype)

    return (
        f"Logged: {event_name} on {event_date}\n"
        f"  Actual:   ${actual_revenue:,.0f}\n"
        f"  Baseline: ${baseline_revenue:,.0f}\n"
        f"  Multiplier: {multiplier:.3f}x\n"
        f"  Learned '{etype}' multiplier: {new_mult:.3f}x (from {count} logged events)"
    )


GET_EVENT_MULTIPLIERS_TOOL = {
    "name": "get_event_multipliers",
    "description": "Show all event type multipliers - defaults and any learned from actual logged outcomes.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


async def handle_get_event_multipliers(arguments: dict) -> str:
    location_id = arguments.get("_location_id", "default")
    learned = _learned_multipliers(location_id)
    outcomes = _load_outcomes(location_id)

    lines = ["EVENT MULTIPLIERS", ""]
    lines.append(f"  {'Event Type':<22}  {'Multiplier':>10}  {'Source':<12}  Data Points")
    lines.append(f"  {'-'*22}  {'-'*10}  {'-'*12}  -----------")

    all_types = set(list(DEFAULT_EVENT_MULTIPLIERS.keys()) + list(learned.keys()))
    for etype in sorted(all_types, key=lambda x: -learned.get(x, DEFAULT_EVENT_MULTIPLIERS.get(x, 1.0))):
        if etype == "default":
            continue
        mult = learned.get(etype, DEFAULT_EVENT_MULTIPLIERS.get(etype, 1.15))
        source = "learned" if etype in learned else "default"
        count = sum(1 for o in outcomes if o.get("event_type") == etype)
        lines.append(f"  {etype:<22}  {mult:>10.3f}x  {source:<12}  {count if count else '-'}")

    lines += ["", f"Total logged event days: {len(outcomes)}", "Use log_event_outcome to improve accuracy."]
    return "\n".join(lines)


TOOLS = [GET_UPCOMING_EVENTS_TOOL, LOG_EVENT_OUTCOME_TOOL, GET_EVENT_MULTIPLIERS_TOOL]

HANDLERS = {
    "get_upcoming_events": handle_get_upcoming_events,
    "log_event_outcome": handle_log_event_outcome,
    "get_event_multipliers": handle_get_event_multipliers,
}
