"""
PrepCast - Upload Report Tool

Ingests daily sales reports from CSV, XLS/XLSX, PDF, or plain-text paste.
Normalizes to {date, revenue} records and appends to sales_history.json.

Supported formats:
  1. CSV with date + revenue columns (auto-detected)
  2. XLSX/XLS spreadsheets (auto-detects date + revenue columns)
  3. PDF with tabular sales data (extracts tables via pdfplumber)
  4. "date: $amount" line format
  5. JSON array of {date, revenue} objects
  6. Base64-encoded file content (for MCP file transfer)

Data validation:
  - Revenue sanity check ($100 - $50,000 range for Five Guys)
  - Duplicate date detection (newer overwrites older)
  - Date continuity gap detection (flags missing days)
  - Future date rejection
"""

import base64
import csv
import io
import json
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from store import load_json, save_json

SALES_FILE = "sales_history.json"

# Revenue sanity bounds (Five Guys single store)
MIN_REVENUE = 100       # below this is probably a parsing error
MAX_REVENUE = 50_000    # above this is probably a parsing error


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
        "%d-%b-%Y", "%d-%b-%y",
        "%Y/%m/%d",
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
        val = float(s)
        return val if val >= 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_records(records: List[Dict]) -> Tuple[List[Dict], List[str]]:
    """
    Validate parsed records and return (clean_records, warnings).
    - Rejects future dates
    - Flags revenue outside reasonable range
    - Flags duplicate dates
    - Detects date gaps
    """
    clean = []
    warnings = []
    today = date.today()
    seen_dates = set()

    for r in records:
        d = r.get("date", "")
        rev = r.get("revenue", 0)

        # Future date
        try:
            if date.fromisoformat(d) > today:
                warnings.append(f"  SKIPPED {d}: future date")
                continue
        except ValueError:
            warnings.append(f"  SKIPPED: invalid date '{d}'")
            continue

        # Revenue sanity
        if rev < MIN_REVENUE:
            warnings.append(f"  WARNING {d}: revenue ${rev:,.0f} seems too low (< ${MIN_REVENUE}). Included but flagged.")
        elif rev > MAX_REVENUE:
            warnings.append(f"  WARNING {d}: revenue ${rev:,.0f} seems too high (> ${MAX_REVENUE:,}). Included but flagged.")

        # Duplicate
        if d in seen_dates:
            warnings.append(f"  DUPLICATE {d}: latest value ${rev:,.0f} will overwrite earlier entry")
        seen_dates.add(d)

        clean.append(r)

    # Gap detection
    if len(clean) >= 7:
        dates_sorted = sorted(set(r["date"] for r in clean))
        gaps = []
        for i in range(1, len(dates_sorted)):
            d1 = date.fromisoformat(dates_sorted[i - 1])
            d2 = date.fromisoformat(dates_sorted[i])
            gap_days = (d2 - d1).days
            if gap_days > 3:  # more than 3 days missing
                gaps.append(f"{dates_sorted[i-1]} to {dates_sorted[i]} ({gap_days} days)")
        if gaps:
            warnings.append(f"  DATE GAPS found ({len(gaps)}): " + "; ".join(gaps[:3]))

    return clean, warnings


# ---------------------------------------------------------------------------
# CSV Parser (existing, enhanced)
# ---------------------------------------------------------------------------

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
        r_candidates = [j for j, c in enumerate(row_lower) if any(k in c for k in ("revenue", "sales", "total", "amount", "net", "gross"))]
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


# ---------------------------------------------------------------------------
# XLSX Parser
# ---------------------------------------------------------------------------

def _parse_xlsx(file_bytes: bytes) -> Tuple[List[Dict], List[str]]:
    """Parse an XLSX file. Auto-detects date and revenue columns."""
    try:
        import openpyxl
    except ImportError:
        return [], ["openpyxl not installed. Run: pip install openpyxl"]

    errors = []
    records = []

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        return [], [f"Could not open XLSX file: {e}"]

    # Use the first sheet
    ws = wb.active
    if ws is None:
        return [], ["No sheets found in workbook."]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], ["Spreadsheet is empty."]

    # Find header row (date + revenue columns)
    date_col = None
    rev_col = None
    header_idx = 0

    for i, row in enumerate(rows[:10]):
        for j, cell in enumerate(row):
            if cell is None:
                continue
            cell_str = str(cell).lower().strip()
            if "date" in cell_str and date_col is None:
                date_col = j
            if any(k in cell_str for k in ("revenue", "sales", "total", "amount", "net", "gross")) and rev_col is None:
                rev_col = j
        if date_col is not None and rev_col is not None:
            header_idx = i
            break

    if date_col is None or rev_col is None:
        # Try to auto-detect: first column with dates, first column with numbers
        for i, row in enumerate(rows[:5]):
            for j, cell in enumerate(row):
                if cell is None:
                    continue
                if date_col is None and (isinstance(cell, datetime) or _parse_date(str(cell))):
                    date_col = j
                elif rev_col is None and isinstance(cell, (int, float)) and cell > 50:
                    rev_col = j
            if date_col is not None and rev_col is not None:
                header_idx = max(0, i - 1)
                break

    if date_col is None or rev_col is None:
        return [], ["Could not find date and revenue columns in spreadsheet."]

    for row_num, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or all(c is None for c in row):
            continue

        raw_date = row[date_col] if date_col < len(row) else None
        raw_rev = row[rev_col] if rev_col < len(row) else None

        if raw_date is None or raw_rev is None:
            continue

        # Handle Excel datetime objects
        if isinstance(raw_date, datetime):
            parsed_date = raw_date.date().isoformat()
        elif isinstance(raw_date, date):
            parsed_date = raw_date.isoformat()
        else:
            parsed_date = _parse_date(str(raw_date))

        if isinstance(raw_rev, (int, float)):
            parsed_rev = float(raw_rev)
        else:
            parsed_rev = _parse_revenue(str(raw_rev))

        if not parsed_date:
            errors.append(f"Row {row_num}: could not parse date '{raw_date}'")
            continue
        if parsed_rev is None:
            errors.append(f"Row {row_num}: could not parse revenue '{raw_rev}'")
            continue

        records.append({"date": parsed_date, "revenue": parsed_rev})

    wb.close()
    return records, errors


# ---------------------------------------------------------------------------
# PDF Parser
# ---------------------------------------------------------------------------

def _parse_pdf(file_bytes: bytes) -> Tuple[List[Dict], List[str]]:
    """Extract text from a PDF and find date + revenue data via pattern matching."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return [], ["pypdfium2 not installed. Run: pip install pypdfium2"]

    errors = []
    records = []

    try:
        pdf = pdfium.PdfDocument(file_bytes)
    except Exception as e:
        return [], [f"Could not open PDF: {e}"]

    all_text = ""
    for page in pdf:
        textpage = page.get_textpage()
        text = textpage.get_text_bounded() or ""
        all_text += text + "\n"
        textpage.close()
        page.close()
    pdf.close()

    if not all_text.strip():
        return [], ["PDF appears to be image-based (no extractable text). Save as CSV or XLSX instead."]

    # Strategy 1: Look for lines with date + dollar amount
    # Patterns: "01/15/2025  $6,200.00" or "January 15, 2025    6200" etc.
    date_amount_pattern = re.compile(
        r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}|'
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})'
        r'\s+\$?([\d,]+\.?\d*)',
        re.IGNORECASE
    )

    for match in date_amount_pattern.finditer(all_text):
        d = _parse_date(match.group(1))
        r = _parse_revenue(match.group(2))
        if d and r is not None and r > 0:
            records.append({"date": d, "revenue": r})

    # Strategy 2: If no direct matches, try line-by-line with more flexible parsing
    if not records:
        for line in all_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Try to find any date-like token followed by a number
            tokens = re.split(r'\s{2,}|\t', line)  # split on 2+ spaces or tabs
            if len(tokens) >= 2:
                d = _parse_date(tokens[0])
                if d:
                    # Find the first numeric token after the date
                    for t in tokens[1:]:
                        r = _parse_revenue(t)
                        if r is not None and r > 0:
                            records.append({"date": d, "revenue": r})
                            break

    if not records:
        # Show a sample of what we extracted so the user can help debug
        sample_lines = [l.strip() for l in all_text.splitlines() if l.strip()][:5]
        errors.append("No date+revenue data found in PDF text.")
        if sample_lines:
            errors.append("First few lines extracted:")
            for sl in sample_lines:
                errors.append(f"  {sl[:80]}")
        errors.append("Try saving as CSV or XLSX for more reliable parsing.")

    return records, errors


# ---------------------------------------------------------------------------
# Unified ingestion: handles text, base64, or raw bytes
# ---------------------------------------------------------------------------

def _detect_and_parse(
    text: str = "",
    file_bytes: bytes = b"",
    filename: str = "",
) -> Tuple[List[Dict], List[str], str]:
    """
    Auto-detect format and parse.
    Returns (records, errors, format_name).
    """
    fmt = "unknown"

    # If we have file bytes, detect format from filename or magic bytes
    if file_bytes:
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

        if ext in ("xlsx", "xls") or file_bytes[:4] == b"PK\x03\x04":
            records, errors = _parse_xlsx(file_bytes)
            return records, errors, "xlsx"

        if ext == "pdf" or file_bytes[:5] == b"%PDF-":
            records, errors = _parse_pdf(file_bytes)
            return records, errors, "pdf"

        if ext == "csv" or ext == "tsv":
            text = file_bytes.decode("utf-8", errors="ignore")
            # Fall through to CSV parsing below

    # Text-based parsing
    if text:
        # Try JSON first
        try:
            data = json.loads(text)
            if isinstance(data, list) and data and "date" in data[0]:
                records = []
                for item in data:
                    d = _parse_date(str(item.get("date", "")))
                    r = item.get("revenue")
                    if d and r is not None:
                        records.append({"date": d, "revenue": float(r)})
                return records, [], "json"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Try CSV
        records, errors = _parse_csv(text)
        if records:
            return records, errors, "csv"

        # Try line-by-line "date: $amount" format
        line_records = []
        for line in text.strip().splitlines():
            m = re.match(r"(.+?)[\s:,\t]+\$?([\d,]+\.?\d*)", line.strip())
            if m:
                d = _parse_date(m.group(1))
                r = _parse_revenue(m.group(2))
                if d and r:
                    line_records.append({"date": d, "revenue": r})
        if line_records:
            return line_records, [], "text_lines"

        return [], errors or ["Could not parse any records from text input."], "unknown"

    return [], ["No data provided."], "unknown"


# ===========================================================================
# MCP Tool: upload_sales_data (unified — CSV, XLSX, PDF, text)
# ===========================================================================

UPLOAD_SALES_TOOL = {
    "name": "upload_sales_data",
    "description": (
        "Import daily sales history from CSV, Excel (XLSX), PDF, or text paste. "
        "For files: pass base64-encoded content in file_base64 with a filename. "
        "For text: paste CSV or line-format data in csv_text. "
        "Auto-detects date and revenue columns in any format. "
        "Validates data: flags suspicious revenue, duplicate dates, and date gaps. "
        "Merges with existing history (duplicates overwritten by latest upload)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "csv_text": {
                "type": "string",
                "description": "Raw text content (CSV, TSV, or 'date: $amount' lines).",
            },
            "file_base64": {
                "type": "string",
                "description": "Base64-encoded file content (for XLSX, PDF, or CSV files).",
            },
            "filename": {
                "type": "string",
                "description": "Original filename (e.g. 'sales_report.xlsx'). Helps detect format.",
            },
        },
        "required": [],
    },
}


async def handle_upload_sales_data(arguments: dict) -> str:
    csv_text = arguments.get("csv_text", "")
    file_b64 = arguments.get("file_base64", "")
    filename = arguments.get("filename", "")
    location_id = arguments.get("_location_id", "default")

    if not csv_text and not file_b64:
        return (
            "No data provided. Either:\n"
            "  1. Paste CSV/text in csv_text\n"
            "  2. Send base64-encoded file in file_base64 with filename\n"
            "  3. Upload a file via POST /upload endpoint"
        )

    # Decode base64 if provided
    file_bytes = b""
    if file_b64:
        try:
            file_bytes = base64.b64decode(file_b64)
        except Exception as e:
            return f"Could not decode base64 file content: {e}"

    records, parse_errors, fmt = _detect_and_parse(
        text=csv_text,
        file_bytes=file_bytes,
        filename=filename,
    )

    if not records:
        return (
            f"Could not parse any records (format: {fmt}).\n"
            + ("\n".join(parse_errors[:5]) if parse_errors else "")
            + "\n\nSupported formats: CSV, XLSX, PDF with date+revenue columns, "
            "or lines like '01/15/2025, $6200'"
        )

    # Validate
    clean_records, validation_warnings = _validate_records(records)

    if not clean_records:
        return (
            "All records failed validation:\n" + "\n".join(validation_warnings[:10])
        )

    # Save
    existing = _load_sales(location_id)
    combined = existing + clean_records
    _save_sales(combined, location_id)
    final = _load_sales(location_id)

    # Summary stats
    revenues = [r["revenue"] for r in clean_records]
    avg_rev = statistics.mean(revenues) if revenues else 0

    lines = [
        f"IMPORTED {len(clean_records)} records from {fmt.upper()}",
        f"  Date range:    {clean_records[0]['date']} -> {clean_records[-1]['date']}",
        f"  Avg revenue:   ${avg_rev:,.0f}/day",
        f"  Min:           ${min(revenues):,.0f}",
        f"  Max:           ${max(revenues):,.0f}",
        f"  Total history: {len(final)} days",
    ]

    if parse_errors:
        lines.append(f"")
        lines.append(f"PARSE ISSUES ({len(parse_errors)}):")
        for e in parse_errors[:5]:
            lines.append(f"  {e}")
        if len(parse_errors) > 5:
            lines.append(f"  ... and {len(parse_errors) - 5} more")

    if validation_warnings:
        lines.append(f"")
        lines.append(f"VALIDATION ({len(validation_warnings)}):")
        for w in validation_warnings[:5]:
            lines.append(w)
        if len(validation_warnings) > 5:
            lines.append(f"  ... and {len(validation_warnings) - 5} more")

    skipped = len(records) - len(clean_records)
    if skipped:
        lines.append(f"")
        lines.append(f"  {skipped} records skipped (future dates or invalid)")

    lines.append(f"")
    lines.append(f"Run analyze_history to see patterns, or forecast_sales to project revenue.")

    return "\n".join(lines)


# Keep backward compatibility: old tool name still works
async def handle_upload_sales_csv(arguments: dict) -> str:
    """Backward-compatible wrapper."""
    return await handle_upload_sales_data(arguments)


UPLOAD_SALES_CSV_TOOL = {
    "name": "upload_sales_csv",
    "description": (
        "Import daily sales history from a CSV or text paste. "
        "Alias for upload_sales_data — use that for XLSX and PDF support too."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "csv_text": {
                "type": "string",
                "description": "Raw CSV content. Example: 'Date,Revenue\\n2025-01-01,6200\\n2025-01-02,5800'",
            },
        },
        "required": ["csv_text"],
    },
}


# ===========================================================================
# Tool: log_daily_sales
# ===========================================================================

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

    rev = float(revenue)
    warnings = []
    if rev < MIN_REVENUE:
        warnings.append(f"  Note: ${rev:,.0f} seems low for a Five Guys day. Double-check?")
    if rev > MAX_REVENUE:
        warnings.append(f"  Note: ${rev:,.0f} seems high for a single store. Double-check?")

    record = {"date": parsed_date, "revenue": rev}
    if notes:
        record["notes"] = notes

    existing = _load_sales(location_id)
    existing.append(record)
    _save_sales(existing, location_id)
    final = _load_sales(location_id)

    result = (
        f"Logged: {parsed_date}  ${rev:,.0f}"
        + (f"  ({notes})" if notes else "")
        + f"\nTotal history: {len(final)} days."
    )
    if warnings:
        result += "\n" + "\n".join(warnings)
    return result


# ===========================================================================
# Tool: get_sales_summary
# ===========================================================================

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
            "Use upload_sales_data to import your spreadsheets (CSV, XLSX, or PDF),\n"
            "or log_daily_sales to add days manually."
        )

    revenues = [r["revenue"] for r in records if "revenue" in r]
    dates = sorted(r["date"] for r in records if "date" in r)

    # Check for gaps
    gap_count = 0
    for i in range(1, len(dates)):
        d1 = date.fromisoformat(dates[i - 1])
        d2 = date.fromisoformat(dates[i])
        gap = (d2 - d1).days
        if gap > 3:
            gap_count += 1

    result = (
        f"SALES HISTORY SUMMARY\n"
        f"  Records:       {len(records)} days\n"
        f"  Date range:    {dates[0]} -> {dates[-1]}\n"
        f"  Avg revenue:   ${statistics.mean(revenues):,.0f}/day\n"
        f"  Median:        ${statistics.median(revenues):,.0f}/day\n"
        f"  Total revenue: ${sum(revenues):,.0f}\n"
        f"  Best day:      ${max(revenues):,.0f}\n"
        f"  Worst day:     ${min(revenues):,.0f}\n"
    )
    if gap_count:
        result += f"  Date gaps:     {gap_count} (runs of 3+ missing days)\n"
    result += f"\nRun analyze_history for day-of-week breakdown."
    return result


# ===========================================================================
# Exports
# ===========================================================================

TOOLS = [UPLOAD_SALES_TOOL, UPLOAD_SALES_CSV_TOOL, LOG_DAILY_SALES_TOOL, GET_SALES_SUMMARY_TOOL]

HANDLERS = {
    "upload_sales_data": handle_upload_sales_data,
    "upload_sales_csv": handle_upload_sales_csv,
    "log_daily_sales": handle_log_daily_sales,
    "get_sales_summary": handle_get_sales_summary,
}
