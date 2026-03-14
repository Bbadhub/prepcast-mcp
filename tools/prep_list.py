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
    update_menu_ratios   - manager calibrates ratios from actual outcomes
    get_menu_ratios      - inspect current ratio config
"""

from typing import Any, Dict
from datetime import date

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

from store import load_json_dict, save_json

RATIOS_FILE = "menu_ratios.json"

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
        "Generate a Five Guys daily prep list from a projected revenue figure. "
        "Returns quantities in bulk units the crew actually uses: cases of beef, "
        "sheets of bacon, sleeves of cheese, bags of potatoes. "
        "Rounded to quarter-unit increments. Over-prep carries to next day. "
        "Add a buffer percentage for event days to avoid running out."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "projected_revenue": {
                "type": "number",
                "description": "Projected daily revenue in dollars (e.g. 6200).",
            },
            "buffer_pct": {
                "type": "number",
                "description": "Safety buffer % to add (default: 10). Use 15-20 for event days.",
            },
            "date": {
                "type": "string",
                "description": "Date for this prep list YYYY-MM-DD (optional, for records).",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes (e.g. 'volleyball tournament 7k attendees').",
            },
        },
        "required": ["projected_revenue"],
    },
}


def _round_quarter(n: float) -> float:
    """Round to nearest 0.25 increment (e.g., 3.1 -> 3.25, 3.6 -> 3.5)."""
    return round(n * 4) / 4


def _fmt_bulk(qty: float) -> str:
    """Format a bulk quantity: show whole number or .25/.5/.75."""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


async def handle_generate_prep_list(arguments: dict) -> str:
    projected_revenue = float(arguments.get("projected_revenue", 0))
    buffer_pct = float(arguments.get("buffer_pct", 10.0))
    date_str = arguments.get("date", "") or "today"
    notes = arguments.get("notes", "")
    location_id = arguments.get("_location_id", "default")

    if not projected_revenue or projected_revenue <= 0:
        return "projected_revenue is required and must be greater than 0."

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

    lines = [
        f"FIVE GUYS PREP LIST - {date_str}",
        f"  Projected Revenue:  ${projected_revenue:,.0f}  (+{buffer_pct:.0f}% buffer)",
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
# Exports
# ===========================================================================

TOOLS = [GENERATE_PREP_LIST_TOOL, UPDATE_RATIOS_TOOL, GET_RATIOS_TOOL]

HANDLERS = {
    "generate_prep_list": handle_generate_prep_list,
    "update_menu_ratios": handle_update_menu_ratios,
    "get_menu_ratios": handle_get_menu_ratios,
}
