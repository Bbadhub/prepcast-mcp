"""
PrepCast — Prep List Tool

Converts a projected daily revenue figure into item-level prep quantities.
Uses configurable menu ratios (how much of each item sells per $1000 revenue)
that get refined over time as actual vs predicted data accumulates.

Menu items modeled:
  - Burger patties (shared: cheeseburger + bacon burger)
  - Bacon strips (probabilistic split — hardest one)
  - Hot dogs
  - Grilled cheese
  - Buns (burger + hot dog)
  - Cheese slices
  - Condiment packs (estimated)

Handlers:
    generate_prep_list   — full prep list for a projected revenue
    update_menu_ratios   — manager calibrates ratios from actual outcomes
    get_menu_ratios      — inspect current ratio config
"""

import json
import os
from typing import Any, Dict
from datetime import date

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("PREPCAST_DATA_DIR", "/data/prepcast")
RATIOS_FILE = os.path.join(DATA_DIR, "menu_ratios.json")

# Default ratios: units per $1,000 revenue
# These are starter estimates — manager should calibrate with real data
DEFAULT_RATIOS = {
    "burger_patties_per_1k": 28,       # total patties (cheese + bacon burgers)
    "bacon_burger_pct": 0.40,          # 40% of burgers ordered as bacon burgers
    "bacon_strips_per_bacon_burger": 3, # strips per bacon burger
    "hot_dogs_per_1k": 18,
    "grilled_cheese_per_1k": 12,
    "burger_buns_per_1k": 28,          # matches patties
    "hotdog_buns_per_1k": 18,          # matches hot dogs
    "cheese_slices_per_1k": 35,        # burgers + grilled cheese
    "condiment_packs_per_1k": 40,
    # Case conversion (David's formula: $500 = 1 case patties)
    "patty_case_cost": 500,
    "patty_case_count": 80,            # patties per case (adjust to your supplier)
}


def _load_ratios() -> Dict:
    if os.path.exists(RATIOS_FILE):
        try:
            with open(RATIOS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_RATIOS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_RATIOS)


def _save_ratios(ratios: Dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RATIOS_FILE, "w") as f:
        json.dump(ratios, f, indent=2)


# ===========================================================================
# Tool: generate_prep_list
# ===========================================================================

GENERATE_PREP_LIST_TOOL = {
    "name": "generate_prep_list",
    "description": (
        "Generate a complete daily prep list from a projected revenue figure. "
        "Returns quantities for all menu items: patties, bacon, hot dogs, "
        "grilled cheese, buns, cheese slices, and condiments. "
        "Also shows case counts using the $500/case formula. "
        "Include a buffer percentage to avoid running out during rushes."
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
                "description": (
                    "Safety buffer percentage to add on top of projection (default: 10). "
                    "Use 15-20 for event days, 5-10 for normal days."
                ),
            },
            "date": {
                "type": "string",
                "description": "Date for this prep list in YYYY-MM-DD format (for records).",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes (e.g. 'volleyball tournament 7k attendees').",
            },
        },
        "required": ["projected_revenue"],
    },
}


async def handle_generate_prep_list(
    projected_revenue: float = 0,
    buffer_pct: float = 10.0,
    date: str = "",
    notes: str = "",
) -> str:
    if not projected_revenue or projected_revenue <= 0:
        return "projected_revenue is required and must be greater than 0."

    ratios = _load_ratios()
    rev_k = projected_revenue / 1000.0
    buf = 1 + (buffer_pct / 100.0)

    # --- Calculate quantities ---
    patties_raw = rev_k * ratios["burger_patties_per_1k"]
    patties = int(patties_raw * buf)

    bacon_burgers = int(patties_raw * ratios["bacon_burger_pct"])
    plain_burgers = int(patties_raw * (1 - ratios["bacon_burger_pct"]))
    bacon_strips = int(bacon_burgers * ratios["bacon_strips_per_bacon_burger"] * buf)

    hot_dogs = int(rev_k * ratios["hot_dogs_per_1k"] * buf)
    grilled_cheese = int(rev_k * ratios["grilled_cheese_per_1k"] * buf)
    burger_buns = patties  # 1:1
    hotdog_buns = hot_dogs
    cheese_slices = int(rev_k * ratios["cheese_slices_per_1k"] * buf)
    condiments = int(rev_k * ratios["condiment_packs_per_1k"] * buf)

    # --- Case conversion ---
    patty_cases = patties / ratios["patty_case_count"]
    case_cost = patty_cases * ratios["patty_case_cost"]

    date_str = date or "today"
    lines = [
        f"PREP LIST — {date_str}",
        f"  Projected Revenue:  ${projected_revenue:,.0f}  (+{buffer_pct:.0f}% buffer)",
        *(["  Notes: " + notes] if notes else []),
        f"",
        f"PROTEINS",
        f"  Burger Patties:     {patties:>5}  ({patty_cases:.1f} cases  ~${case_cost:,.0f})",
        f"    ↳ Cheeseburgers:  {plain_burgers:>5}  ({100*(1-ratios['bacon_burger_pct']):.0f}% of burgers)",
        f"    ↳ Bacon Burgers:  {bacon_burgers:>5}  ({100*ratios['bacon_burger_pct']:.0f}% of burgers)",
        f"  Bacon Strips:       {bacon_strips:>5}  (est. — actual split varies)",
        f"  Hot Dogs:           {hot_dogs:>5}",
        f"",
        f"BREAD",
        f"  Burger Buns:        {burger_buns:>5}",
        f"  Hot Dog Buns:       {hotdog_buns:>5}",
        f"",
        f"DAIRY / OTHER",
        f"  Cheese Slices:      {cheese_slices:>5}",
        f"  Grilled Cheese:     {grilled_cheese:>5}",
        f"  Condiment Packs:    {condiments:>5}",
        f"",
        f"NOTE: Bacon split is probabilistic (±20%). Ratios update as you log actuals.",
        f"Use update_menu_ratios to calibrate after each day.",
    ]
    return "\n".join(lines)


# ===========================================================================
# Tool: update_menu_ratios
# ===========================================================================

UPDATE_RATIOS_TOOL = {
    "name": "update_menu_ratios",
    "description": (
        "Update the prep ratio for a specific menu item based on actual outcomes. "
        "For example, if you consistently run out of bacon strips, increase "
        "bacon_strips_per_bacon_burger. Over time this tunes the model to your store."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ratio_key": {
                "type": "string",
                "description": (
                    "Which ratio to update. Options: burger_patties_per_1k, "
                    "bacon_burger_pct, bacon_strips_per_bacon_burger, "
                    "hot_dogs_per_1k, grilled_cheese_per_1k, cheese_slices_per_1k, "
                    "condiment_packs_per_1k, patty_case_cost, patty_case_count."
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


async def handle_update_menu_ratios(ratio_key: str = "", value: float = 0) -> str:
    if not ratio_key:
        return "ratio_key is required."
    ratios = _load_ratios()
    if ratio_key not in ratios:
        valid = ", ".join(ratios.keys())
        return f"Unknown ratio key '{ratio_key}'. Valid keys: {valid}"
    old = ratios[ratio_key]
    ratios[ratio_key] = value
    _save_ratios(ratios)
    return f"Updated {ratio_key}: {old} → {value}\nSaved to {RATIOS_FILE}"


# ===========================================================================
# Tool: get_menu_ratios
# ===========================================================================

GET_RATIOS_TOOL = {
    "name": "get_menu_ratios",
    "description": "Show current prep ratios used to generate prep lists. Use this to review and calibrate.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle_get_menu_ratios() -> str:
    ratios = _load_ratios()
    lines = ["CURRENT PREP RATIOS (units per $1,000 revenue unless noted)", ""]
    for k, v in ratios.items():
        lines.append(f"  {k:<40} {v}")
    lines += ["", "Use update_menu_ratios to calibrate based on actual daily outcomes."]
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
