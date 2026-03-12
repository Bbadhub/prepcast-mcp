"""
PrepCast - Upload Report Tool

Ingests daily sales reports from CSV or plain-text paste.
Normalizes to {date, revenue} records and appends to sales_history.json.

Supported formats:
  1. CSV with date + revenue columns (auto-detected)
  2. "date: $amount" line format
  3. JSON array of {date, revenue} objects
"""

import csv
import io
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from store import load_json, save_json

SALES_FILE = "sales_history.json"


def _load_sales(location_id: str = "default") -> List[Dict]:
    return load_json(location_id, SALES_FILE)


def _save_sales(records: List[Dict], location_id: str = "default"):
    by_date = {}
    for r in records:
        by_date[r["date"]] = r
    sorted_records = sorted(by_date.values(), key=lambda x: x["date"])
    save_json(location_id, SALES_FILE, sorted_records)


def _parse_date(s: str) -> Optional[str]:
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
    s = re.sub(r"[$,\s]", "", s.strip())
    try:
        return float(s)
    except ValueError:
        return None


def _parse_csv(text: str) -> Tuple[List[Dict], List[str]]:
    records = []
    errors = []
    reader = csv.reader(io.StringIO(text.strip()))
    rows = list(reader)
    if not rows:
        return [], ["No data found."]

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

    if date_col is None:
        date_col, rev_col = 0, 1
        header_idx = 0
        if rows and not _parse_date(rows[0][0] if rows[0] else ""):
            header_idx = 1

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


UPLOAD_SALES_CSV_TOOL = {
    "name": "upload_sales_csv",
    "description": (
        "Import daily sales history from a CSV or text paste. "
        "Paste your spreadsheet contents directly - the tool auto-detects date and revenue columns. "
        "Merges with existing history (duplicates by date are overwritten). "
        "After uploading, run analyze_history to see patterns."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "csv_text": {
                "type": "string",
                "description": "Raw CSV content. Example: 'Date,Revenue\\n2025-01-01,6200\\n2025-01-02,5800'",
            },
            "location_name": {
                "type": "string",
                "description": "Optional location label (e.g. 'Overland Park 163rd').",
            },
        },
        "required": ["csv_text"],
    },
}


async def handle_upload_sales_csv(arguments: dict) -> str:
    csv_text = arguments.get("csv_text", "")
    location_name = arguments.get("location_name", "")
    location_id = arguments.get("_location_id", "default")

    if not csv_text or not csv_text.strip():
        return "csv_text is required. Paste your spreadsheet contents."

    records, errors = _parse_csv(csv_text)

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
            "Could not parse any records.\n"
            "Expected: CSV with Date and Revenue columns, or lines like '01/15/2025, $6200'\n"
            + ("\nErrors:\n" + "\n".join(errors[:5]) if errors else "")
        )

    if location_name:
        for r in records:
            r["location"] = location_name

    existing = _load_sales(location_id)
    combined = existing + records
    _save_sales(combined, location_id)
    final = _load_sales(location_id)

    return (
        f"Imported {len(records)} records.\n"
        f"  Date range: {records[0]['date']} -> {records[-1]['date']}\n"
        f"  Total history: {len(final)} days\n"
        + (f"  Warnings ({len(errors)}):\n    " + "\n    ".join(errors[:3]) if errors else "")
        + f"\n\nRun analyze_history to see patterns, or forecast_sales to project revenue."
    )


LOG_DAILY_SALES_TOOL = {
    "name": "log_daily_sales",
    "description": "Log a single day's actual sales. Use at end of day to keep history current.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD or MM/DD/YYYY format."},
            "revenue": {"type": "number", "description": "Total revenue for the day in dollars."},
            "notes": {"type": "string", "description": "Optional notes (e.g. 'ran out of bacon', 'big tournament')."},
        },
        "required": ["date", "revenue"],
    },
}


async def handle_log_daily_sales(arguments: dict) -> str:
    date_str = arguments.get("date", "")
    revenue = arguments.get("revenue", 0)
    notes = arguments.get("notes", "")
    location_id = arguments.get("_location_id", "default")

    if not date_str or not revenue:
        return "date and revenue are required."

    parsed_date = _parse_date(str(date_str))
    if not parsed_date:
        return f"Could not parse date '{date_str}'. Use YYYY-MM-DD or MM/DD/YYYY."

    record = {"date": parsed_date, "revenue": float(revenue)}
    if notes:
        record["notes"] = notes

    existing = _load_sales(location_id)
    existing.append(record)
    _save_sales(existing, location_id)
    final = _load_sales(location_id)

    return (
        f"Logged: {parsed_date}  ${float(revenue):,.0f}"
        + (f"  ({notes})" if notes else "")
        + f"\nTotal history: {len(final)} days."
    )


GET_SALES_SUMMARY_TOOL = {
    "name": "get_sales_summary",
    "description": "Quick summary of how much sales history is loaded and what date range it covers.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


async def handle_get_sales_summary(arguments: dict) -> str:
    location_id = arguments.get("_location_id", "default")
    records = _load_sales(location_id)
    if not records:
        return (
            "No sales history loaded.\n"
            "Use upload_sales_csv to import your spreadsheets, or log_daily_sales to add days manually."
        )

    revenues = [r["revenue"] for r in records if "revenue" in r]
    dates = sorted(r["date"] for r in records if "date" in r)

    return (
        f"SALES HISTORY SUMMARY\n"
        f"  Records:       {len(records)} days\n"
        f"  Date range:    {dates[0]} -> {dates[-1]}\n"
        f"  Avg revenue:   ${sum(revenues)/len(revenues):,.0f}/day\n"
        f"  Total revenue: ${sum(revenues):,.0f}\n"
        f"  Best day:      ${max(revenues):,.0f}\n"
        f"  Worst day:     ${min(revenues):,.0f}\n\n"
        f"Run analyze_history for day-of-week breakdown."
    )


TOOLS = [UPLOAD_SALES_CSV_TOOL, LOG_DAILY_SALES_TOOL, GET_SALES_SUMMARY_TOOL]

HANDLERS = {
    "upload_sales_csv": handle_upload_sales_csv,
    "log_daily_sales": handle_log_daily_sales,
    "get_sales_summary": handle_get_sales_summary,
}
