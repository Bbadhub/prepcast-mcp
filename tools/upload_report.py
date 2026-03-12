"""
PrepCast — Upload Report Tool

Ingests daily sales reports from CSV or plain-text paste.
Normalizes to a standard record format and appends to sales_history.json.

Supported input formats:
  1. CSV text paste (date, revenue columns)
  2. Simple "date: $amount" line format (easy manual entry)
  3. JSON array of {date, revenue} objects

Handlers:
    upload_sales_csv    — ingest CSV/text sales data
    log_daily_sales     — log a single day manually
    get_sales_summary   — quick summary of stored history
    clear_sales_history — wipe and start fresh
"""

import csv
import io
import json
import os
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("PREPCAST_DATA_DIR", "/data/prepcast")
SALES_FILE = os.path.join(DATA_DIR, "sales_history.json")


def _load_sales() -> List[Dict]:
    if not os.path.exists(SALES_FILE):
        return []
    try:
        with open(SALES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_sales(records: List[Dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    # Sort by date ascending, deduplicate by date (keep last)
    by_date = {}
    for r in records:
        by_date[r["date"]] = r
    sorted_records = sorted(by_date.values(), key=lambda x: x["date"])
    with open(SALES_FILE, "w") as f:
        json.dump(sorted_records, f, indent=2)


def _parse_date(s: str) -> Optional[str]:
    """Try multiple date formats, return ISO string or None."""
    s = s.strip().strip('"').strip("'")
    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y",
        "%m-%d-%Y", "%m-%d-%y",
        "%B %d, %Y", "%b %d, %Y",
        "%b %d %Y", "%B %d %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_revenue(s: str) -> Optional[float]:
    """Strip $, commas, whitespace from revenue string."""
    s = re.sub(r"[$,\s]", "", s.strip())
    try:
        return float(s)
    except ValueError:
        return None


def _parse_csv(text: str) -> Tuple[List[Dict], List[str]]:
    """
    Parse CSV text into records.
    Auto-detects date + revenue columns.
    Returns (records, errors).
    """
    records = []
    errors = []

    reader = csv.reader(io.StringIO(text.strip()))
    rows = list(reader)

    if not rows:
        return [], ["No data found in CSV."]

    # Find header row — look for columns containing "date" and "revenue"/"sales"/"amount"
    header = None
    header_idx = 0
    date_col = None
    rev_col = None

    for i, row in enumerate(rows[:5]):
        row_lower = [c.lower().strip() for c in row]
        d_candidates = [j for j, c in enumerate(row_lower) if "date" in c]
        r_candidates = [j for j, c in enumerate(row_lower) if any(k in c for k in ("revenue", "sales", "total", "amount", "net"))]
        if d_candidates and r_candidates:
            header = row
            header_idx = i
            date_col = d_candidates[0]
            rev_col = r_candidates[0]
            break

    # No header found — assume first col is date, second is revenue
    if date_col is None:
        date_col, rev_col = 0, 1
        header_idx = 0
        # Check if first row looks like a header
        if rows and not _parse_date(rows[0][0] if rows[0] else ""):
            header_idx = 1  # skip header row

    data_rows = rows[header_idx + (1 if header else 0):]

    for line_no, row in enumerate(data_rows, start=header_idx + 2):
        if not row or all(c.strip() == "" for c in row):
            continue
        try:
            raw_date = row[date_col] if date_col < len(row) else ""
            raw_rev = row[rev_col] if rev_col < len(row) else ""
        except IndexError:
            errors.append(f"Row {line_no}: not enough columns")
            continue

        parsed_date = _parse_date(raw_date)
        parsed_rev = _parse_revenue(raw_rev)

        if not parsed_date:
            errors.append(f"Row {line_no}: could not parse date '{raw_date}'")
            continue
        if parsed_rev is None:
            errors.append(f"Row {line_no}: could not parse revenue '{raw_rev}'")
            continue

        records.append({"date": parsed_date, "revenue": parsed_rev})

    return records, errors


# ===========================================================================
# Tool: upload_sales_csv
# ===========================================================================

UPLOAD_SALES_CSV_TOOL = {
    "name": "upload_sales_csv",
    "description": (
        "Import daily sales history from a CSV or text paste. "
        "Paste the contents of your spreadsheet directly — "
        "the tool auto-detects date and revenue columns. "
        "Supports formats: CSV, tab-separated, or 'date: $amount' lines. "
        "Merges with existing history (duplicates by date are overwritten)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "csv_text": {
                "type": "string",
                "description": (
                    "Raw CSV or text content of your sales report. "
                    "Example: 'Date,Revenue\\n2025-01-01,$6200\\n2025-01-02,$5800'"
                ),
            },
            "location_name": {
                "type": "string",
                "description": "Optional location label (e.g. 'Overland Park 163rd'). Stored with records.",
            },
        },
        "required": ["csv_text"],
    },
}


async def handle_upload_sales_csv(csv_text: str = "", location_name: str = "") -> str:
    if not csv_text or not csv_text.strip():
        return "csv_text is required. Paste your spreadsheet contents."

    # Try CSV first
    records, errors = _parse_csv(csv_text)

    # If CSV parse got nothing, try "date: $amount" line format
    if not records:
        line_records = []
        for line in csv_text.strip().splitlines():
            m = re.match(r"(.+?)[\s:,\t]+\$?([\d,]+\.?\d*)", line.strip())
            if m:
                d = _parse_date(m.group(1))
                r = _parse_revenue(m.group(2))
                if d and r:
                    line_records.append({"date": d, "revenue": r})
        if line_records:
            records = line_records
            errors = []

    if not records:
        return (
            "Could not parse any records from the input.\n"
            "Expected format: CSV with Date and Revenue columns, or lines like '01/15/2025, $6200'\n"
            + ("\nErrors:\n" + "\n".join(errors[:5]) if errors else "")
        )

    # Add location tag if provided
    if location_name:
        for r in records:
            r["location"] = location_name

    existing = _load_sales()
    combined = existing + records
    _save_sales(combined)

    final = _load_sales()
    return (
        f"Imported {len(records)} records.\n"
        f"  Date range: {records[0]['date']} → {records[-1]['date']}\n"
        f"  Total history: {len(final)} days\n"
        + (f"  Warnings ({len(errors)}):\n    " + "\n    ".join(errors[:3]) if errors else "")
        + f"\n\nRun analyze_history to see patterns, or forecast_sales to project revenue."
    )


# ===========================================================================
# Tool: log_daily_sales
# ===========================================================================

LOG_DAILY_SALES_TOOL = {
    "name": "log_daily_sales",
    "description": (
        "Log a single day's actual sales. Use this at end of day to keep "
        "history current. Overwrites any existing entry for the same date."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD or MM/DD/YYYY format.",
            },
            "revenue": {
                "type": "number",
                "description": "Total revenue for the day in dollars.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes (e.g. 'ran out of bacon mid-day', 'big volleyball tournament').",
            },
        },
        "required": ["date", "revenue"],
    },
}


async def handle_log_daily_sales(date: str = "", revenue: float = 0, notes: str = "") -> str:
    if not date or not revenue:
        return "date and revenue are required."

    parsed_date = _parse_date(date)
    if not parsed_date:
        return f"Could not parse date '{date}'. Use YYYY-MM-DD or MM/DD/YYYY."

    record = {"date": parsed_date, "revenue": revenue}
    if notes:
        record["notes"] = notes

    existing = _load_sales()
    existing.append(record)
    _save_sales(existing)
    final = _load_sales()

    return (
        f"Logged: {parsed_date}  ${revenue:,.0f}"
        + (f"  ({notes})" if notes else "")
        + f"\nTotal history: {len(final)} days."
    )


# ===========================================================================
# Tool: get_sales_summary
# ===========================================================================

GET_SALES_SUMMARY_TOOL = {
    "name": "get_sales_summary",
    "description": "Quick summary of how much sales history is loaded and what date range it covers.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def handle_get_sales_summary() -> str:
    records = _load_sales()
    if not records:
        return (
            "No sales history loaded.\n"
            "Use upload_sales_csv to import your spreadsheets, or log_daily_sales to add days manually."
        )

    revenues = [r["revenue"] for r in records if "revenue" in r]
    dates = sorted(r["date"] for r in records if "date" in r)

    return (
        f"SALES HISTORY SUMMARY\n"
        f"  Records:      {len(records)} days\n"
        f"  Date range:   {dates[0]} → {dates[-1]}\n"
        f"  Avg revenue:  ${sum(revenues)/len(revenues):,.0f}/day\n"
        f"  Total revenue: ${sum(revenues):,.0f}\n"
        f"  Min day:       ${min(revenues):,.0f}\n"
        f"  Max day:       ${max(revenues):,.0f}\n\n"
        f"Run analyze_history for day-of-week breakdown."
    )


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [UPLOAD_SALES_CSV_TOOL, LOG_DAILY_SALES_TOOL, GET_SALES_SUMMARY_TOOL]

HANDLERS = {
    "upload_sales_csv": handle_upload_sales_csv,
    "log_daily_sales": handle_log_daily_sales,
    "get_sales_summary": handle_get_sales_summary,
}
