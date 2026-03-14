"""
PrepCast - Prep List Tool (Five Guys Edition)

Converts a projected daily revenue into item-level prep quantities
calibrated to the Five Guys menu: fresh-never-frozen beef patties,
bacon, hot dogs, sandwiches, fries, buns, cheese, and toppings.

Five Guys key facts:
  - Standard burger = 2 patties (3.3 oz each pre-cook)
  - Little burger = 1 patty
  - Bacon: Applewood smoked, prepped in full-day batches, must be crispy
  - Fries: Double-fried from fresh Burbank/Norkotah potatoes in peanut oil
  - 15 free toppings - grilled onions + mushrooms require active prep
  - BLT = exactly 6 strips of bacon
  - No freezer, no microwave - everything fresh daily

Handlers:
    generate_prep_list   - full Five Guys prep list for a projected revenue
    log_prep_outcome     - crew logs actual usage at end of day; auto-calibrates ratios
    update_menu_ratios   - manager manually overrides a ratio
    get_menu_ratios      - inspect current ratio config
"""

from typing import Any, Dict, List
from datetime import date, datetime, timedelta
import statistics

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

from store import load_json, load_json_dict, save_json

RATIOS_FILE = "menu_ratios.json"
PREP_OUTCOMES_FILE = "prep_outcomes.json"

# ---------------------------------------------------------------------------
# Five Guys Default Ratios - units per $1,000 revenue
#
# Based on:
#   - Standard burger = 2 patties, little burger = 1 patty
#   - ~55% standard burgers, ~35% little burgers, ~10% other
#   - Average ticket ~$15-18 (burger + fries + drink)
#   - Bacon on ~50% of all burger orders
#   - Hot dogs ~8% of orders, grilled cheese/BLT/veggie ~6%
#   - Fries ordered with ~85% of meals
# ---------------------------------------------------------------------------

DEFAULT_RATIOS = {
    # ---- PROTEINS ----
    # Patties calibrated from operator data:
    #   $3,000 sales -> 4-5 cases at $500/case -> ~540 patties
    #   ~180 patties per $1,000 in sales
    # Standard burger (2 patties) + little burger (1 patty), blended ~1.7 patties/order
    "patties_per_1k":               180,
    # Split: 55% standard (2 patties), 35% little (1 patty), 10% other
    "standard_burger_pct":          0.55,
    "little_burger_pct":            0.35,
    # Bacon: ~50% of all burger orders get bacon (Applewood smoked, 2 strips)
    "bacon_burger_pct":             0.50,
    "bacon_strips_per_burger":      2,
    # Hot dogs: operator data shows <20/day regardless of volume.
    # At a $5k avg day that's <4 per $1k. Using 3/1k as conservative real-world floor.
    "hot_dogs_per_1k":              3,
    "bacon_dog_pct":                0.40,
    "bacon_strips_per_dog":         2,
    # BLT sandwich per $1k (exactly 6 strips bacon each) — rare item
    "blt_per_1k":                   2,
    "bacon_strips_per_blt":         6,
    # Grilled cheese per $1k
    "grilled_cheese_per_1k":        4,
    # Veggie sandwich per $1k
    "veggie_sandwich_per_1k":       2,

    # ---- BREAD ----
    # Burger orders = patties / blended patties-per-order (1.7)
    "patties_per_order_blended":    1.7,
    "hotdog_buns_per_1k":           3,    # tracks hot_dogs_per_1k

    # ---- DAIRY ----
    # Cheese: ~70% of burgers get cheese (1 slice each); grilled cheese = 2 slices
    # At 180 patties/$1k -> ~106 orders/$1k -> ~74 cheese slices + ~8 grilled cheese = ~82/1k
    "cheese_slices_per_1k":         82,

    # ---- FRIES ----
    # Five Guys Cajun/regular fries — ordered with most meals
    # ~0.9 lb raw potato per portion; ~85% of orders include fries
    "fry_portions_per_1k":          52,
    "potato_lbs_per_portion":       0.90,

    # ---- TOPPINGS (active prep items) ----
    "grilled_onion_portions_per_1k":  18,
    "grilled_mushroom_portions_per_1k": 9,
    "tomato_slices_per_1k":           40,
    "lettuce_portions_per_1k":        40,

    # ---- CASE / BULK UNIT CONVERSIONS ----
    # Patty case: ~120 patties (3.3 oz each, 40 lb case)
    # Operator-confirmed: $500/case
    "patty_case_lbs":               40,
    "patties_per_case":             120,
    "patty_case_cost":              500,  # operator-confirmed $500/case
    # Bacon: 15 lb case, ~240 strips; a "sheet" (sheet pan) = ~20 strips
    "bacon_strips_per_case":        240,
    "bacon_strips_per_sheet":       20,
    "bacon_case_cost":              55,
    # Cheese: American cheese sleeve/stack = ~72 slices
    "cheese_slices_per_sleeve":     72,
    # Hot dogs: case of 48 (Hebrew National)
    "hotdogs_per_case":             48,
    "hotdog_case_cost":             35,
    # Potato: 50 lb bag; 1 bag = 2 buckets
    "potato_lbs_per_bag":           50,
    "potato_bag_cost":              22,
}


def _load_ratios(location_id: str = "default") -> Dict:
    saved = load_json_dict(location_id, RATIOS_FILE)
    if saved:
        return {**DEFAULT_RATIOS, **saved}
    return dict(DEFAULT_RATIOS)


def _save_ratios(ratios: Dict, location_id: str = "default"):
    save_json(location_id, RATIOS_FILE, ratios)


# ===========================================================================
# Tool: generate_prep_list
# ===========================================================================

GENERATE_PREP_LIST_TOOL = {
    "name": "generate_prep_list",
    "description": (
        "Generate a Five Guys daily prep list. If no projected_revenue is given, "
        "the system auto-forecasts from your sales history (day-of-week patterns, "
        "recent trends) and checks weather to adjust. Returns quantities in bulk "
        "units: cases of beef, sheets of bacon, sleeves of cheese, bags of potatoes. "
        "Rounded to quarter-unit increments. Over-prep carries to next day."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "projected_revenue": {
                "type": "number",
                "description": "Projected daily revenue in dollars. Leave blank to auto-forecast from sales history.",
            },
            "buffer_pct": {
                "type": "number",
                "description": "Safety buffer % to add (default: 10). Auto-increases for events/bad weather.",
            },
            "date": {
                "type": "string",
                "description": "Date for this prep list YYYY-MM-DD. Defaults to tomorrow.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes (e.g. 'volleyball tournament 7k attendees').",
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Auto-forecast: pull revenue from sales history + weather + events
# ---------------------------------------------------------------------------

def _auto_forecast_revenue(target_date: date, location_id: str) -> Dict:
    """
    Use sales history to auto-forecast revenue for a target date.
    Returns dict with 'revenue', 'method', 'weather', 'event_multiplier', etc.
    """
    from tools.sales_forecast import _load_sales, _recency_weighted_forecast, _baseline_by_dow, _recent_trend

    records = _load_sales(location_id)
    result = {
        "revenue": None,
        "method": "none",
        "weather_condition": None,
        "weather_multiplier": 1.0,
        "event_multiplier": 1.0,
        "notes": [],
    }

    if not records:
        return result

    dow = target_date.strftime("%A")

    # Primary: recency-weighted (same method as forecast_sales)
    projected = _recency_weighted_forecast(records, dow, target_date)
    baselines = _baseline_by_dow(records)
    base = baselines.get(dow)

    if projected is not None:
        result["revenue"] = projected
        result["method"] = f"history-weighted ({dow})"
        result["notes"].append(f"{dow} baseline: ${base:,.0f}" if base else f"Using recency model")
    elif base:
        trend = _recent_trend(records)
        projected = base * (trend if trend else 1.0)
        result["revenue"] = projected
        result["method"] = f"DOW baseline + trend"
    else:
        # Fallback: overall average
        all_vals = [float(r["revenue"]) for r in records if "revenue" in r]
        if all_vals:
            result["revenue"] = statistics.mean(all_vals)
            result["method"] = "overall average (thin history)"

    if result["revenue"] is None:
        return result

    # --- Weather adjustment ---
    try:
        from tools.weather import _fetch_weather, _condition_from_wmo, WEATHER_MULTIPLIERS, DEFAULT_LAT, DEFAULT_LON
        weather_data = _fetch_weather(DEFAULT_LAT, DEFAULT_LON, target_date.isoformat())
        if "error" not in weather_data:
            daily = weather_data["daily"]
            wmo_code = daily["weathercode"][0]
            temp_max = daily["temperature_2m_max"][0]
            condition = _condition_from_wmo(wmo_code, temp_max)
            mult = WEATHER_MULTIPLIERS.get(condition, 1.0)
            result["weather_condition"] = condition
            result["weather_multiplier"] = mult
            result["revenue"] *= mult
            if mult != 1.0:
                result["notes"].append(f"Weather: {condition.replace('_', ' ')} ({mult:.2f}x)")
    except Exception:
        pass  # weather unavailable, proceed without it

    # --- Event adjustment (check logged event multipliers) ---
    try:
        from tools.events import _learned_multipliers, DEFAULT_EVENT_MULTIPLIERS
        learned = _learned_multipliers(location_id)
        # Check prep outcomes for same date's event context
        outcomes = _load_prep_outcomes(location_id)
        # Check if there's a logged event for this date or nearby
        for o in outcomes:
            if o.get("date") == target_date.isoformat() and o.get("event"):
                from tools.events import _classify_event
                etype = _classify_event(o["event"])
                mult = learned.get(etype, DEFAULT_EVENT_MULTIPLIERS.get(etype, 1.15))
                result["event_multiplier"] = mult
                result["revenue"] *= mult
                result["notes"].append(f"Event: {o['event']} ({mult:.2f}x)")
                break
    except Exception:
        pass

    return result


def _round_quarter(n: float) -> float:
    """Round to nearest 0.25 increment (e.g., 3.1 -> 3.25, 3.6 -> 3.5)."""
    return round(n * 4) / 4


def _fmt_bulk(qty: float) -> str:
    """Format a bulk quantity: show whole number or .25/.5/.75."""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


async def handle_generate_prep_list(arguments: dict) -> str:
    projected_revenue = float(arguments.get("projected_revenue", 0) or 0)
    buffer_pct = arguments.get("buffer_pct")
    date_str = arguments.get("date", "")
    notes = arguments.get("notes", "")
    location_id = arguments.get("_location_id", "default")

    # Default to tomorrow if no date given
    if not date_str:
        target_date = date.today() + timedelta(days=1)
        date_str = target_date.isoformat()
    else:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            target_date = date.today() + timedelta(days=1)

    forecast_info = []

    # --- Auto-forecast if no revenue given ---
    if not projected_revenue or projected_revenue <= 0:
        auto = _auto_forecast_revenue(target_date, location_id)
        if auto["revenue"] is None or auto["revenue"] <= 0:
            return (
                "No projected revenue given and not enough sales history to auto-forecast.\n"
                "Either provide projected_revenue, or upload sales data first with upload_sales_csv."
            )
        projected_revenue = auto["revenue"]
        forecast_info.append(f"  Auto-forecast:  ${projected_revenue:,.0f}  ({auto['method']})")
        if auto["weather_condition"]:
            forecast_info.append(f"  Weather:        {auto['weather_condition'].replace('_', ' ')}  ({auto['weather_multiplier']:.2f}x)")
        if auto["event_multiplier"] != 1.0:
            forecast_info.append(f"  Event boost:    {auto['event_multiplier']:.2f}x")
        for n in auto["notes"]:
            forecast_info.append(f"  -> {n}")

        # Auto-adjust buffer for bad weather
        if buffer_pct is None:
            if auto.get("weather_multiplier", 1.0) < 0.85:
                buffer_pct = 5.0  # less buffer when it's raining (less traffic)
            elif auto.get("event_multiplier", 1.0) > 1.10:
                buffer_pct = 15.0  # more buffer on event days
            else:
                buffer_pct = 10.0
    else:
        if buffer_pct is None:
            buffer_pct = 10.0

    r = _load_ratios(location_id)
    k = projected_revenue / 1000.0
    buf = 1 + (buffer_pct / 100.0)

    # ---- Burger order count estimate ----
    patties_raw = k * r["patties_per_1k"]
    blended = r.get("patties_per_order_blended", 1.7)
    burger_orders = patties_raw / blended

    # ---- Beef (CASES) ----
    patties = k * r["patties_per_1k"] * buf
    patty_cases_raw = patties / r["patties_per_case"]
    patty_cases = _round_quarter(patty_cases_raw)
    patty_cost = patty_cases * r["patty_case_cost"]

    # ---- Bacon (SHEETS) ----
    bacon_burgers = burger_orders * r["bacon_burger_pct"]
    hot_dogs = k * r["hot_dogs_per_1k"] * buf
    bacon_dogs = hot_dogs * r["bacon_dog_pct"]
    blts = k * r["blt_per_1k"] * buf

    bacon_strips = (
        bacon_burgers * r["bacon_strips_per_burger"] +
        bacon_dogs * r["bacon_strips_per_dog"] +
        blts * r["bacon_strips_per_blt"]
    ) * buf
    bacon_sheets_raw = bacon_strips / r.get("bacon_strips_per_sheet", 20)
    bacon_sheets = _round_quarter(bacon_sheets_raw)
    bacon_cases_raw = bacon_strips / r["bacon_strips_per_case"]
    bacon_cases = _round_quarter(bacon_cases_raw)
    bacon_cost = bacon_cases * r["bacon_case_cost"]

    # ---- Hot Dogs (CASES) ----
    hotdog_cases_raw = hot_dogs / r["hotdogs_per_case"]
    hotdog_cases = _round_quarter(hotdog_cases_raw)
    hotdog_cost = hotdog_cases * r["hotdog_case_cost"]

    # ---- Cheese (SLEEVES / STACKS) ----
    cheese_slices = k * r["cheese_slices_per_1k"] * buf
    cheese_sleeves_raw = cheese_slices / r.get("cheese_slices_per_sleeve", 72)
    cheese_sleeves = _round_quarter(cheese_sleeves_raw)

    # ---- Fries / Potatoes (BAGS) ----
    fry_portions = k * r["fry_portions_per_1k"] * buf
    potato_lbs = fry_portions * r["potato_lbs_per_portion"]
    potato_bags_raw = potato_lbs / r["potato_lbs_per_bag"]
    potato_bags = _round_quarter(potato_bags_raw)
    potato_cost = potato_bags * r["potato_bag_cost"]
    # 1 bag = 2 buckets
    potato_buckets = potato_bags * 2

    # ---- Other sandwich counts (for context) ----
    grilled_cheese = int(k * r["grilled_cheese_per_1k"] * buf)
    veggie = int(k * r["veggie_sandwich_per_1k"] * buf)
    blt_count = int(blts)

    # ---- Bread ----
    burger_buns = int((burger_orders + grilled_cheese + veggie) * buf)
    hotdog_buns = int(hot_dogs)

    # ---- Toppings ----
    grilled_onions = int(k * r["grilled_onion_portions_per_1k"] * buf)
    grilled_mushrooms = int(k * r["grilled_mushroom_portions_per_1k"] * buf)

    total_food_cost = patty_cost + bacon_cost + hotdog_cost + potato_cost

    day_name = target_date.strftime("%A")
    lines = [
        f"FIVE GUYS PREP LIST - {day_name} {date_str}",
        f"  Projected Revenue:  ${projected_revenue:,.0f}  (+{buffer_pct:.0f}% buffer)",
        *forecast_info,
        *(["  Notes: " + notes] if notes else []),
        f"",
        f"--- WHAT TO PULL / PREP --------------------------------",
        f"  Beef:               {_fmt_bulk(patty_cases)} cases          ~${patty_cost:,.0f}",
        f"  Bacon:              {_fmt_bulk(bacon_sheets)} sheets  ({_fmt_bulk(bacon_cases)} cases  ~${bacon_cost:,.0f})",
        f"  Cheese:             {_fmt_bulk(cheese_sleeves)} sleeves",
        f"  Potatoes:           {_fmt_bulk(potato_bags)} bags  ({_fmt_bulk(potato_buckets)} buckets)  ~${potato_cost:,.0f}",
        f"  Hot Dogs:           {_fmt_bulk(hotdog_cases)} cases          ~${hotdog_cost:,.0f}",
        f"",
        f"--- BREAD ----------------------------------------------",
        f"  Sesame Buns:        {burger_buns}",
        f"  Hot Dog Buns:       {hotdog_buns}",
        f"",
        f"--- SANDWICH COUNTS (for reference) --------------------",
        f"  Grilled Cheese:     {grilled_cheese}",
        f"  Veggie Sandwich:    {veggie}",
        f"  BLT:                {blt_count}",
        f"",
        f"--- ACTIVE TOPPINGS (prep before open) -----------------",
        f"  Grilled Onions:     {grilled_onions} portions",
        f"  Grilled Mushrooms:  {grilled_mushrooms} portions",
        f"  Jalapenos, green peppers, pickles, tomatoes, lettuce: top up from stock",
        f"",
        f"--- ESTIMATED FOOD COST --------------------------------",
        f"  Proteins + Fries:  ~${total_food_cost:,.0f}",
        f"  Food cost %:       ~{(total_food_cost/projected_revenue)*100:.1f}%  (target: 28-32%)",
        f"",
        f"Quantities rounded to nearest quarter-unit.",
        f"Over-prep carries to next day. Calibrate with update_menu_ratios.",
    ]
    return "\n".join(lines)


# ===========================================================================
# Tool: update_menu_ratios
# ===========================================================================

UPDATE_RATIOS_TOOL = {
    "name": "update_menu_ratios",
    "description": (
        "Update a prep ratio for a specific menu item based on actual outcomes. "
        "Example: if you consistently run short on bacon strips, increase bacon_strips_per_burger. "
        "Over time this tunes the model to your specific store's order mix."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ratio_key": {
                "type": "string",
                "description": (
                    "Which ratio to update. Key options: patties_per_1k, bacon_burger_pct, "
                    "bacon_strips_per_burger, hot_dogs_per_1k, fry_portions_per_1k, "
                    "grilled_onion_portions_per_1k, patty_case_cost, patties_per_case."
                ),
            },
            "value": {
                "type": "number",
                "description": "New value for the ratio.",
            },
        },
        "required": ["ratio_key", "value"],
    },
}


async def handle_update_menu_ratios(arguments: dict) -> str:
    ratio_key = arguments.get("ratio_key", "")
    value = arguments.get("value")
    location_id = arguments.get("_location_id", "default")
    if not ratio_key:
        return "ratio_key is required."
    if value is None:
        return "value is required."
    ratios = _load_ratios(location_id)
    if ratio_key not in ratios:
        valid = ", ".join(list(ratios.keys())[:10]) + "..."
        return f"Unknown ratio key '{ratio_key}'. Valid keys include: {valid}"
    old = ratios[ratio_key]
    ratios[ratio_key] = float(value)
    _save_ratios(ratios, location_id)
    return f"Updated {ratio_key}: {old} -> {value}"


# ===========================================================================
# Tool: get_menu_ratios
# ===========================================================================

GET_RATIOS_TOOL = {
    "name": "get_menu_ratios",
    "description": "Show current Five Guys prep ratios used to generate prep lists. Use this to review and calibrate.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle_get_menu_ratios(arguments: dict) -> str:
    location_id = arguments.get("_location_id", "default")
    ratios = _load_ratios(location_id)
    lines = ["FIVE GUYS PREP RATIOS", ""]
    sections = {
        "PROTEINS (units per $1k revenue)": [
            "patties_per_1k", "standard_burger_pct", "little_burger_pct",
            "bacon_burger_pct", "bacon_strips_per_burger",
            "hot_dogs_per_1k", "bacon_dog_pct", "bacon_strips_per_dog",
            "blt_per_1k", "bacon_strips_per_blt",
            "grilled_cheese_per_1k", "veggie_sandwich_per_1k",
        ],
        "BREAD & DAIRY": [
            "burger_buns_per_1k", "hotdog_buns_per_1k", "cheese_slices_per_1k",
        ],
        "FRIES": [
            "fry_portions_per_1k", "potato_lbs_per_portion",
        ],
        "TOPPINGS": [
            "grilled_onion_portions_per_1k", "grilled_mushroom_portions_per_1k",
            "tomato_slices_per_1k", "lettuce_portions_per_1k",
        ],
        "BULK UNITS & COSTS": [
            "patties_per_case", "patty_case_cost",
            "bacon_strips_per_case", "bacon_strips_per_sheet", "bacon_case_cost",
            "cheese_slices_per_sleeve",
            "hotdogs_per_case", "hotdog_case_cost",
            "potato_lbs_per_bag", "potato_bag_cost",
        ],
    }
    for section, keys in sections.items():
        lines.append(f"  {section}")
        for k in keys:
            if k in ratios:
                lines.append(f"    {k:<42} {ratios[k]}")
        lines.append("")
    lines.append("Use update_menu_ratios to calibrate based on actual daily outcomes.")
    return "\n".join(lines)


# ===========================================================================
# Tool: log_prep_outcome  (end-of-day feedback loop)
# ===========================================================================

LOG_PREP_OUTCOME_TOOL = {
    "name": "log_prep_outcome",
    "description": (
        "Log what the crew actually used at end of day. The system compares this "
        "to what was prepped and auto-adjusts ratios over time so prep lists get "
        "more accurate. Uses the same bulk units: cases of beef, sheets of bacon, "
        "sleeves of cheese, bags of potatoes. Also records weather and events for "
        "that day so the system learns different patterns for different conditions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date this outcome is for (YYYY-MM-DD).",
            },
            "actual_revenue": {
                "type": "number",
                "description": "Actual revenue for the day.",
            },
            "beef_cases_used": {
                "type": "number",
                "description": "How many cases of beef were actually used (e.g. 9.5).",
            },
            "bacon_sheets_used": {
                "type": "number",
                "description": "How many sheets of bacon were actually used.",
            },
            "cheese_sleeves_used": {
                "type": "number",
                "description": "How many sleeves of cheese were actually used.",
            },
            "potato_bags_used": {
                "type": "number",
                "description": "How many bags of potatoes were actually used.",
            },
            "ran_out_of": {
                "type": "string",
                "description": "Comma-separated items that ran out (e.g. 'bacon,cheese'). Leave blank if nothing ran out.",
            },
            "weather": {
                "type": "string",
                "description": "Weather that day (e.g. 'rain', 'clear', 'hot'). Optional - system can auto-fetch.",
            },
            "event": {
                "type": "string",
                "description": "Event nearby that day (e.g. 'volleyball tournament'). Blank if none.",
            },
            "notes": {
                "type": "string",
                "description": "Any notes (e.g. 'short staffed', 'late truck delivery').",
            },
        },
        "required": ["date", "actual_revenue"],
    },
}


def _load_prep_outcomes(location_id: str = "default") -> List[Dict]:
    return load_json(location_id, PREP_OUTCOMES_FILE)


def _save_prep_outcomes(outcomes: List[Dict], location_id: str = "default"):
    save_json(location_id, PREP_OUTCOMES_FILE, outcomes)


def _auto_calibrate(outcomes: List[Dict], ratios: Dict, location_id: str) -> List[str]:
    """
    Compare prep outcomes to what the ratios would have predicted.
    If there's a consistent bias over 5+ data points, nudge the ratio.
    Returns a list of adjustment messages.

    Learning rules:
      - Only adjust if 5+ outcomes logged (need pattern, not noise)
      - Compare actual usage per $1k revenue vs current ratio prediction
      - Nudge ratio 20% toward observed value (conservative — don't overfit)
      - Cap any single adjustment at +/-10% of current value
      - Track weather/event context: only learn from "normal" days for base ratios
      - Detect day-of-week patterns (e.g. Saturdays use more bacon)
    """
    adjustments = []

    # Filter to normal days (no events, no extreme weather) for base ratio learning
    normal_outcomes = [
        o for o in outcomes
        if not o.get("event") and o.get("weather", "clear") not in ("rain", "heavy_rain", "thunderstorm", "snow", "blizzard")
    ]

    if len(normal_outcomes) < 5:
        return adjustments

    def _nudge_ratio(key: str, observed: float, label: str):
        """Nudge a ratio 20% toward observed, capped at +/-10%."""
        current = ratios.get(key, observed)
        if current == 0:
            return
        if abs(observed - current) / current > 0.03:  # >3% off
            nudge = current + (observed - current) * 0.20
            nudge = max(current * 0.90, min(current * 1.10, nudge))
            nudge = round(nudge, 1)
            if nudge != current:
                ratios[key] = nudge
                adjustments.append(f"  {key}: {current} -> {nudge} ({label})")

    # --- Beef: compare cases used vs cases predicted ---
    beef_data = [
        o for o in normal_outcomes
        if o.get("beef_cases_used") is not None and o.get("actual_revenue", 0) > 0
    ]
    if len(beef_data) >= 5:
        observed_per_1k = [
            (o["beef_cases_used"] * ratios.get("patties_per_case", 120)) / (o["actual_revenue"] / 1000)
            for o in beef_data[-15:]
        ]
        _nudge_ratio("patties_per_1k", statistics.mean(observed_per_1k), "beef usage pattern")

    # --- Bacon: compare sheets used ---
    bacon_data = [
        o for o in normal_outcomes
        if o.get("bacon_sheets_used") is not None and o.get("actual_revenue", 0) > 0
    ]
    if len(bacon_data) >= 5:
        # Use ran_out_of as strong signal for under-prepping
        ran_out_bacon = sum(1 for o in bacon_data[-15:] if "bacon" in (o.get("ran_out_of") or "").lower())
        if ran_out_bacon >= 2:
            old = ratios.get("bacon_burger_pct", 0.50)
            new = min(old * 1.05, 0.80)
            ratios["bacon_burger_pct"] = round(new, 3)
            adjustments.append(f"  bacon_burger_pct: {old} -> {new:.3f} (ran out {ran_out_bacon}x recently)")

    # --- Cheese: compare sleeves used ---
    cheese_data = [
        o for o in normal_outcomes
        if o.get("cheese_sleeves_used") is not None and o.get("actual_revenue", 0) > 0
    ]
    if len(cheese_data) >= 5:
        slices_per_sleeve = ratios.get("cheese_slices_per_sleeve", 72)
        observed_per_1k = [
            (o["cheese_sleeves_used"] * slices_per_sleeve) / (o["actual_revenue"] / 1000)
            for o in cheese_data[-15:]
        ]
        _nudge_ratio("cheese_slices_per_1k", statistics.mean(observed_per_1k), "cheese usage pattern")

    # --- Potatoes: compare bags used ---
    potato_data = [
        o for o in normal_outcomes
        if o.get("potato_bags_used") is not None and o.get("actual_revenue", 0) > 0
    ]
    if len(potato_data) >= 5:
        lbs_per_bag = ratios.get("potato_lbs_per_bag", 50)
        lbs_per_portion = ratios.get("potato_lbs_per_portion", 0.90)
        observed_per_1k = [
            (o["potato_bags_used"] * lbs_per_bag / lbs_per_portion) / (o["actual_revenue"] / 1000)
            for o in potato_data[-15:]
        ]
        _nudge_ratio("fry_portions_per_1k", statistics.mean(observed_per_1k), "potato usage pattern")

    # --- Day-of-week pattern detection ---
    # If certain days consistently use more/less of an item, flag it
    dow_beef: Dict[str, List[float]] = {}
    for o in normal_outcomes:
        if o.get("beef_cases_used") is not None and o.get("actual_revenue", 0) > 0:
            try:
                d = date.fromisoformat(o["date"])
                dow = d.strftime("%A")
                per_1k = (o["beef_cases_used"] * ratios.get("patties_per_case", 120)) / (o["actual_revenue"] / 1000)
                dow_beef.setdefault(dow, []).append(per_1k)
            except (ValueError, KeyError):
                continue

    if dow_beef:
        overall_avg = statistics.mean([v for vals in dow_beef.values() for v in vals])
        for dow, vals in dow_beef.items():
            if len(vals) >= 3:
                dow_avg = statistics.mean(vals)
                diff_pct = (dow_avg - overall_avg) / overall_avg * 100
                if abs(diff_pct) > 8:
                    direction = "more" if diff_pct > 0 else "less"
                    adjustments.append(f"  PATTERN: {dow}s use {abs(diff_pct):.0f}% {direction} beef than average")

    # --- Ran-out-of signals (any item) ---
    recent = normal_outcomes[-15:]
    for item in ("beef", "bacon", "cheese", "potato", "potatoes", "fries"):
        ran_out_count = sum(1 for o in recent if item in (o.get("ran_out_of") or "").lower())
        if ran_out_count >= 3:
            adjustments.append(f"  WARNING: Ran out of {item} {ran_out_count}x in last 15 days. Consider increasing buffer_pct.")

    if adjustments:
        _save_ratios(ratios, location_id)

    return adjustments


async def handle_log_prep_outcome(arguments: dict) -> str:
    date_str = arguments.get("date", "")
    actual_revenue = float(arguments.get("actual_revenue", 0))
    location_id = arguments.get("_location_id", "default")

    if not date_str or not actual_revenue:
        return "date and actual_revenue are required."

    outcome = {
        "date": date_str,
        "actual_revenue": actual_revenue,
        "logged_at": datetime.utcnow().isoformat(),
    }

    # Bulk usage fields (all optional)
    for field in ("beef_cases_used", "bacon_sheets_used", "cheese_sleeves_used", "potato_bags_used"):
        val = arguments.get(field)
        if val is not None:
            outcome[field] = float(val)

    # Context fields
    for field in ("ran_out_of", "weather", "event", "notes"):
        val = arguments.get(field)
        if val:
            outcome[field] = val

    # Save outcome
    outcomes = _load_prep_outcomes(location_id)
    # Replace if same date exists
    outcomes = [o for o in outcomes if o.get("date") != date_str]
    outcomes.append(outcome)
    outcomes.sort(key=lambda o: o.get("date", ""))
    _save_prep_outcomes(outcomes, location_id)

    # Run auto-calibration
    ratios = _load_ratios(location_id)
    adjustments = _auto_calibrate(outcomes, ratios, location_id)

    lines = [
        f"Logged prep outcome for {date_str}",
        f"  Revenue: ${actual_revenue:,.0f}",
    ]
    if outcome.get("beef_cases_used") is not None:
        lines.append(f"  Beef used: {_fmt_bulk(outcome['beef_cases_used'])} cases")
    if outcome.get("bacon_sheets_used") is not None:
        lines.append(f"  Bacon used: {_fmt_bulk(outcome['bacon_sheets_used'])} sheets")
    if outcome.get("cheese_sleeves_used") is not None:
        lines.append(f"  Cheese used: {_fmt_bulk(outcome['cheese_sleeves_used'])} sleeves")
    if outcome.get("potato_bags_used") is not None:
        lines.append(f"  Potatoes used: {_fmt_bulk(outcome['potato_bags_used'])} bags")
    if outcome.get("ran_out_of"):
        lines.append(f"  Ran out of: {outcome['ran_out_of']}")
    if outcome.get("weather"):
        lines.append(f"  Weather: {outcome['weather']}")
    if outcome.get("event"):
        lines.append(f"  Event: {outcome['event']}")

    lines.append(f"")
    lines.append(f"Total logged days: {len(outcomes)}")

    if adjustments:
        lines.append(f"")
        lines.append(f"AUTO-CALIBRATION (ratios adjusted):")
        lines.extend(adjustments)
    elif len(outcomes) < 5:
        lines.append(f"Auto-calibration activates after 5 logged days ({5 - len(outcomes)} more needed).")
    else:
        lines.append(f"Ratios look accurate - no adjustments needed.")

    return "\n".join(lines)


# ===========================================================================
# Tool: get_prep_dashboard  (in-chat visual artifact data)
# ===========================================================================

GET_PREP_DASHBOARD_TOOL = {
    "name": "get_prep_dashboard",
    "description": (
        "Get prep efficiency data for in-chat visual display. Returns structured data "
        "showing: prep accuracy trends over time, food cost tracking, what items run "
        "out most, auto-calibration history, and weather/event impact on prep needs. "
        "Use this to show the operator visual charts and tables right in the conversation."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days of history to include (default: 30).",
            },
        },
        "required": [],
    },
}


async def handle_get_prep_dashboard(arguments: dict) -> str:
    location_id = arguments.get("_location_id", "default")
    days = int(arguments.get("days", 30))
    outcomes = _load_prep_outcomes(location_id)
    ratios = _load_ratios(location_id)

    if not outcomes:
        return (
            "NO PREP DATA YET\n\n"
            "Start logging end-of-day usage with log_prep_outcome to build your dashboard.\n"
            "After 5+ days, the system auto-calibrates your ratios from actual patterns.\n\n"
            "Example: log_prep_outcome with date, actual_revenue, beef_cases_used, "
            "bacon_sheets_used, cheese_sleeves_used, potato_bags_used"
        )

    cutoff = date.today() - __import__("datetime").timedelta(days=days)
    recent = [o for o in outcomes if o.get("date", "") >= cutoff.isoformat()]
    if not recent:
        recent = outcomes[-30:]  # fallback to last 30 entries

    # --- Prep accuracy by item (predicted vs used) ---
    accuracy_data = []
    for o in recent:
        rev = o.get("actual_revenue", 0)
        if not rev:
            continue
        k = rev / 1000.0
        buf = 1.10  # default buffer
        row = {"date": o["date"], "revenue": rev}

        if o.get("beef_cases_used") is not None:
            predicted = _round_quarter(k * ratios["patties_per_1k"] * buf / ratios["patties_per_case"])
            row["beef_predicted"] = predicted
            row["beef_used"] = o["beef_cases_used"]
            row["beef_diff"] = round(predicted - o["beef_cases_used"], 2)

        if o.get("bacon_sheets_used") is not None:
            # Approximate bacon prediction
            patties_raw = k * ratios["patties_per_1k"]
            burger_orders = patties_raw / ratios.get("patties_per_order_blended", 1.7)
            bacon_strips = (burger_orders * ratios["bacon_burger_pct"] * ratios["bacon_strips_per_burger"]) * buf
            predicted = _round_quarter(bacon_strips / ratios.get("bacon_strips_per_sheet", 20))
            row["bacon_predicted"] = predicted
            row["bacon_used"] = o["bacon_sheets_used"]
            row["bacon_diff"] = round(predicted - o["bacon_sheets_used"], 2)

        if o.get("cheese_sleeves_used") is not None:
            predicted = _round_quarter(k * ratios["cheese_slices_per_1k"] * buf / ratios.get("cheese_slices_per_sleeve", 72))
            row["cheese_predicted"] = predicted
            row["cheese_used"] = o["cheese_sleeves_used"]
            row["cheese_diff"] = round(predicted - o["cheese_sleeves_used"], 2)

        if o.get("potato_bags_used") is not None:
            fry_portions = k * ratios["fry_portions_per_1k"] * buf
            predicted = _round_quarter(fry_portions * ratios["potato_lbs_per_portion"] / ratios["potato_lbs_per_bag"])
            row["potato_predicted"] = predicted
            row["potato_used"] = o["potato_bags_used"]
            row["potato_diff"] = round(predicted - o["potato_bags_used"], 2)

        if o.get("weather"):
            row["weather"] = o["weather"]
        if o.get("event"):
            row["event"] = o["event"]
        if o.get("ran_out_of"):
            row["ran_out_of"] = o["ran_out_of"]

        accuracy_data.append(row)

    # --- Summary stats ---
    beef_diffs = [r["beef_diff"] for r in accuracy_data if "beef_diff" in r]
    bacon_diffs = [r["bacon_diff"] for r in accuracy_data if "bacon_diff" in r]
    cheese_diffs = [r["cheese_diff"] for r in accuracy_data if "cheese_diff" in r]
    potato_diffs = [r["potato_diff"] for r in accuracy_data if "potato_diff" in r]

    def _avg(lst):
        return round(statistics.mean(lst), 2) if lst else None

    # --- Ran-out-of frequency ---
    ran_out_counts = {}
    for o in recent:
        items = (o.get("ran_out_of") or "").lower().split(",")
        for item in items:
            item = item.strip()
            if item:
                ran_out_counts[item] = ran_out_counts.get(item, 0) + 1

    # --- Food cost trend ---
    cost_trend = []
    for o in recent:
        rev = o.get("actual_revenue", 0)
        if not rev:
            continue
        beef_cost = (o.get("beef_cases_used") or 0) * ratios.get("patty_case_cost", 500)
        bacon_cost = ((o.get("bacon_sheets_used") or 0) * ratios.get("bacon_strips_per_sheet", 20) / ratios.get("bacon_strips_per_case", 240)) * ratios.get("bacon_case_cost", 55)
        potato_cost = (o.get("potato_bags_used") or 0) * ratios.get("potato_bag_cost", 22)
        total_cost = beef_cost + bacon_cost + potato_cost
        cost_pct = (total_cost / rev * 100) if rev else 0
        cost_trend.append({
            "date": o["date"],
            "revenue": rev,
            "food_cost": round(total_cost, 0),
            "food_cost_pct": round(cost_pct, 1),
        })

    # --- Build output ---
    lines = [
        f"PREP EFFICIENCY DASHBOARD",
        f"  Period: last {days} days  |  {len(recent)} days logged",
        f"",
    ]

    # Accuracy summary
    lines.append("PREP ACCURACY (predicted - used, positive = over-prep)")
    if beef_diffs:
        lines.append(f"  Beef:     avg {_avg(beef_diffs):+.2f} cases/day  (over {len(beef_diffs)} days)")
    if bacon_diffs:
        lines.append(f"  Bacon:    avg {_avg(bacon_diffs):+.2f} sheets/day")
    if cheese_diffs:
        lines.append(f"  Cheese:   avg {_avg(cheese_diffs):+.2f} sleeves/day")
    if potato_diffs:
        lines.append(f"  Potatoes: avg {_avg(potato_diffs):+.2f} bags/day")
    if not any([beef_diffs, bacon_diffs, cheese_diffs, potato_diffs]):
        lines.append("  No item-level data yet. Log beef_cases_used, bacon_sheets_used, etc.")
    lines.append("")

    # Ran out of
    if ran_out_counts:
        lines.append("RAN OUT OF (frequency)")
        for item, count in sorted(ran_out_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {item:<20} {count}x")
        lines.append("")

    # Food cost trend
    if cost_trend:
        lines.append("FOOD COST TREND")
        for ct in cost_trend[-10:]:
            bar = "#" * int(ct["food_cost_pct"] / 2)
            target_marker = " <-- target" if 28 <= ct["food_cost_pct"] <= 32 else ""
            lines.append(f"  {ct['date']}  ${ct['revenue']:>6,.0f}  cost ${ct['food_cost']:>5,.0f}  {ct['food_cost_pct']:>4.1f}% {bar}{target_marker}")
        lines.append(f"  Target range: 28-32%")
        lines.append("")

    # Daily detail table
    lines.append("DAILY DETAIL")
    lines.append(f"  {'Date':<12} {'Rev':>7} {'Beef':>6} {'Bacon':>6} {'Cheese':>6} {'Potato':>6} {'Weather':>8} {'Event'}")
    lines.append(f"  {'-'*12} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*15}")
    for r in accuracy_data[-15:]:
        beef = f"{r.get('beef_used', '-'):>6}" if isinstance(r.get('beef_used'), (int, float)) else "     -"
        bacon = f"{r.get('bacon_used', '-'):>6}" if isinstance(r.get('bacon_used'), (int, float)) else "     -"
        cheese = f"{r.get('cheese_used', '-'):>6}" if isinstance(r.get('cheese_used'), (int, float)) else "     -"
        potato = f"{r.get('potato_used', '-'):>6}" if isinstance(r.get('potato_used'), (int, float)) else "     -"
        weather = r.get("weather", "-")[:8]
        event = r.get("event", "-")[:15]
        ran_out = f" !! {r['ran_out_of']}" if r.get("ran_out_of") else ""
        lines.append(f"  {r['date']:<12} ${r['revenue']:>6,.0f} {beef} {bacon} {cheese} {potato} {weather:>8} {event}{ran_out}")

    lines.append("")
    lines.append(f"Auto-calibration: {'ACTIVE' if len(outcomes) >= 5 else f'{5 - len(outcomes)} more days needed'}")
    lines.append("Log end-of-day usage with log_prep_outcome to improve accuracy.")

    return "\n".join(lines)


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [GENERATE_PREP_LIST_TOOL, LOG_PREP_OUTCOME_TOOL, GET_PREP_DASHBOARD_TOOL, UPDATE_RATIOS_TOOL, GET_RATIOS_TOOL]

HANDLERS = {
    "generate_prep_list": handle_generate_prep_list,
    "log_prep_outcome": handle_log_prep_outcome,
    "get_prep_dashboard": handle_get_prep_dashboard,
    "update_menu_ratios": handle_update_menu_ratios,
    "get_menu_ratios": handle_get_menu_ratios,
}
