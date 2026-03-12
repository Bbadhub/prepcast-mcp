"""
PrepCast — Events Tool

Fetches upcoming events at AdventHealth Sports Park (Blue Hawk)
at 163rd St in Overland Park, KS, and correlates event type + attendance
to expected foot traffic multipliers.

Also supports manually logging past event outcomes so the system
learns how each event type affects your actual sales.

Handlers:
    get_upcoming_events     — fetch + score upcoming Blue Hawk events
    log_event_outcome       — record actual sales for an event day
    get_event_multipliers   — show learned multipliers per event type
"""

import json
import os
import re
import urllib.request
import urllib.error
from datetime import date, datetime
from typing import Dict, List, Optional
import statistics

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("PREPCAST_DATA_DIR", "/data/prepcast")
EVENT_OUTCOMES_FILE = os.path.join(DATA_DIR, "event_outcomes.json")

# ---------------------------------------------------------------------------
# Known event type multipliers (starter estimates, refined via log_event_outcome)
# These reflect typical fast-food foot traffic bumps near sports complexes
# ---------------------------------------------------------------------------

DEFAULT_EVENT_MULTIPLIERS = {
    "volleyball": 1.45,       # large tournaments, 5k-10k people
    "soccer": 1.35,
    "football": 1.30,
    "baseball": 1.25,
    "softball": 1.25,
    "basketball": 1.20,
    "hockey": 1.20,
    "cornhole": 1.05,         # David said: calm day
    "cheerleading": 1.40,
    "wrestling": 1.30,
    "gymnastics": 1.25,
    "swim": 1.15,
    "track": 1.10,
    "lacrosse": 1.20,
    "default": 1.15,          # unknown event type
}

# Attendance → multiplier curve (override event type if attendance provided)
# Based on: 1000 attendees ≈ +5% foot traffic, capped at +60%
def attendance_to_multiplier(attendance: int) -> float:
    return min(1.0 + (attendance / 1000) * 0.05, 1.60)


def _classify_event(title: str) -> str:
    """Guess event type from title string."""
    title_lower = title.lower()
    for sport in DEFAULT_EVENT_MULTIPLIERS:
        if sport in title_lower:
            return sport
    return "default"


def _load_outcomes() -> List[Dict]:
    if not os.path.exists(EVENT_OUTCOMES_FILE):
        return []
    try:
        with open(EVENT_OUTCOMES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_outcomes(outcomes: List[Dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(EVENT_OUTCOMES_FILE, "w") as f:
        json.dump(outcomes, f, indent=2)


def _learned_multipliers() -> Dict[str, float]:
    """Compute learned multipliers from logged outcomes vs baseline revenue."""
    outcomes = _load_outcomes()
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


# ===========================================================================
# Tool: get_upcoming_events
# ===========================================================================

GET_UPCOMING_EVENTS_TOOL = {
    "name": "get_upcoming_events",
    "description": (
        "Fetch upcoming events at AdventHealth Sports Park (Blue Hawk) "
        "in Overland Park, KS (163rd St). Returns event names, dates, "
        "estimated attendance, and projected foot traffic multiplier for "
        "each event. Use this before forecasting sales on a busy weekend."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days ahead to look for events (default: 14).",
            },
        },
        "required": [],
    },
}


async def handle_get_upcoming_events(days_ahead: int = 14) -> str:
    """
    Attempt to fetch Blue Hawk event data.
    Primary: scrape adventhealthsportspark.com/events (or their API if available).
    Fallback: return guidance for manual entry.
    """
    learned = _learned_multipliers()

    events_found = []
    fetch_note = ""

    # Try fetching from AdventHealth Sports Park
    urls_to_try = [
        "https://www.adventhealthsportspark.com/events",
        "https://www.bluehawksportspark.com/events",
    ]

    raw_html = ""
    for url in urls_to_try:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 PrepCast/1.0 (event calendar fetch)"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_html = resp.read().decode("utf-8", errors="ignore")
                fetch_note = f"Fetched from {url}"
                break
        except Exception as e:
            fetch_note = f"Could not fetch live events ({e}). Showing guidance below."

    # Parse any event titles + dates from HTML (lightweight regex, no BeautifulSoup dep)
    if raw_html:
        # Look for common event title patterns in HTML
        title_matches = re.findall(
            r'(?:class="[^"]*(?:event|title)[^"]*"[^>]*>|<h[123][^>]*>)\s*([A-Za-z][^<]{5,80})</(?:h[123]|[a-z])',
            raw_html,
            re.IGNORECASE,
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
                # Use learned multiplier if available, else default
                mult = learned.get(etype, DEFAULT_EVENT_MULTIPLIERS.get(etype, DEFAULT_EVENT_MULTIPLIERS["default"]))
                event_date = date_matches[i] if i < len(date_matches) else "date TBD"
                events_found.append({
                    "title": title,
                    "date": event_date,
                    "type": etype,
                    "multiplier": mult,
                })

    if events_found:
        lines = [
            f"UPCOMING EVENTS — AdventHealth Sports Park ({fetch_note})",
            f"",
        ]
        for e in events_found[:10]:
            src = "learned" if e["type"] in learned else "estimated"
            lines.append(
                f"  {e['date']:<20}  {e['title'][:40]:<42}  {e['multiplier']:.2f}x  [{src}]"
            )
        lines += [
            f"",
            f"Use these multipliers in forecast_sales → event_multiplier param.",
            f"After the event, log actuals with log_event_outcome to improve accuracy.",
        ]
        return "\n".join(lines)

    # Fallback: manual guidance
    lines = [
        f"ADVENTHEALTH SPORTS PARK — EVENT LOOKUP",
        f"",
        f"  {fetch_note}",
        f"",
        f"  Manual steps:",
        f"  1. Check: https://www.adventhealthsportspark.com/events",
        f"  2. Note event name + expected attendance",
        f"  3. Run forecast_sales with event_name + event_attendance params",
        f"",
        f"ESTIMATED FOOT TRAFFIC MULTIPLIERS BY EVENT TYPE:",
    ]
    all_mults = {**DEFAULT_EVENT_MULTIPLIERS}
    all_mults.update(learned)  # learned data overrides defaults
    for etype, mult in sorted(all_mults.items(), key=lambda x: -x[1]):
        src = " (learned)" if etype in learned else ""
        lines.append(f"  {etype:<20}  {mult:.2f}x{src}")

    lines += [
        f"",
        f"Example: volleyball tournament 7,000 people → use 1.45x multiplier",
        f"         cornhole 600 people → use 1.05x (David says: calm day)",
    ]
    return "\n".join(lines)


# ===========================================================================
# Tool: log_event_outcome
# ===========================================================================

LOG_EVENT_OUTCOME_TOOL = {
    "name": "log_event_outcome",
    "description": (
        "Log the actual sales outcome for an event day. This teaches the system "
        "how each event type really affects your foot traffic over time. "
        "The more you log, the more accurate future forecasts become."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "event_date": {
                "type": "string",
                "description": "Date of the event in YYYY-MM-DD format.",
            },
            "event_name": {
                "type": "string",
                "description": "Name of the event (e.g. 'Regional Volleyball Tournament').",
            },
            "event_type": {
                "type": "string",
                "description": "Type of event (e.g. volleyball, soccer, cornhole).",
            },
            "attendance": {
                "type": "integer",
                "description": "Actual or estimated event attendance.",
            },
            "actual_revenue": {
                "type": "number",
                "description": "What you actually rang up that day.",
            },
            "baseline_revenue": {
                "type": "number",
                "description": "What a normal same-day-of-week would be (no event).",
            },
        },
        "required": ["event_date", "event_name", "actual_revenue", "baseline_revenue"],
    },
}


async def handle_log_event_outcome(
    event_date: str = "",
    event_name: str = "",
    event_type: str = "",
    attendance: int = 0,
    actual_revenue: float = 0,
    baseline_revenue: float = 0,
) -> str:
    if not event_date or not event_name or not actual_revenue or not baseline_revenue:
        return "event_date, event_name, actual_revenue, and baseline_revenue are all required."

    etype = event_type or _classify_event(event_name)
    multiplier = round(actual_revenue / baseline_revenue, 3) if baseline_revenue else 0

    outcomes = _load_outcomes()
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
    _save_outcomes(outcomes)

    # Recompute learned multiplier for this type
    learned = _learned_multipliers()
    new_mult = learned.get(etype, multiplier)

    return (
        f"Logged event outcome for {event_name} on {event_date}.\n"
        f"  Actual revenue:   ${actual_revenue:,.0f}\n"
        f"  Baseline revenue: ${baseline_revenue:,.0f}\n"
        f"  Multiplier:       {multiplier:.3f}x\n"
        f"  Learned '{etype}' multiplier now: {new_mult:.3f}x "
        f"(based on {sum(1 for o in outcomes if o.get('event_type') == etype)} logged events)"
    )


# ===========================================================================
# Tool: get_event_multipliers
# ===========================================================================

GET_EVENT_MULTIPLIERS_TOOL = {
    "name": "get_event_multipliers",
    "description": (
        "Show all event type multipliers — both the defaults and any learned "
        "from actual logged outcomes at this location."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle_get_event_multipliers() -> str:
    learned = _learned_multipliers()
    outcomes = _load_outcomes()

    lines = ["EVENT MULTIPLIERS", ""]
    lines.append(f"  {'Event Type':<22}  {'Multiplier':>10}  {'Source':<12}  {'Data Points'}")
    lines.append(f"  {'-'*22}  {'-'*10}  {'-'*12}  {'-'*11}")

    all_types = set(list(DEFAULT_EVENT_MULTIPLIERS.keys()) + list(learned.keys()))
    for etype in sorted(all_types, key=lambda x: -learned.get(x, DEFAULT_EVENT_MULTIPLIERS.get(x, 1.0))):
        if etype == "default":
            continue
        mult = learned.get(etype, DEFAULT_EVENT_MULTIPLIERS.get(etype, 1.15))
        source = "learned" if etype in learned else "default"
        count = sum(1 for o in outcomes if o.get("event_type") == etype)
        lines.append(f"  {etype:<22}  {mult:>10.3f}x  {source:<12}  {count if count else '—'}")

    lines += [
        "",
        f"Total logged event days: {len(outcomes)}",
        "Use log_event_outcome after each event day to improve accuracy.",
    ]
    return "\n".join(lines)


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [GET_UPCOMING_EVENTS_TOOL, LOG_EVENT_OUTCOME_TOOL, GET_EVENT_MULTIPLIERS_TOOL]

HANDLERS = {
    "get_upcoming_events": handle_get_upcoming_events,
    "log_event_outcome": handle_log_event_outcome,
    "get_event_multipliers": handle_get_event_multipliers,
}
