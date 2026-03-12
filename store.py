"""
PrepCast - Multi-Store Data Layer

All data lives under:
  PREPCAST_DATA_DIR/<location_id>/sales_history.json
  PREPCAST_DATA_DIR/<location_id>/event_outcomes.json
  PREPCAST_DATA_DIR/<location_id>/forecast_log.json
  PREPCAST_DATA_DIR/<location_id>/menu_ratios.json

Legacy single-dir layout (pre-multi-store) is under PREPCAST_DATA_DIR directly
and is treated as location_id="default".

Corporate tokens can access any location_id or use location_id="ALL" to query
across every location.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(os.environ.get("PREPCAST_DATA_DIR", "/tmp/prepcast"))
USERS_FILE = DATA_DIR / "users.json"


def slugify(name: str) -> str:
    """Turn 'Five Guys Overland Park 163rd' into 'five-guys-overland-park-163rd'."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:48] or "default"


def location_dir(location_id: str) -> Path:
    if not location_id or location_id == "default":
        return DATA_DIR  # legacy layout
    safe = re.sub(r"[^a-z0-9\-_]", "", location_id.lower())[:48] or "default"
    p = DATA_DIR / "locations" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_location_ids() -> List[str]:
    """Return all known location IDs (legacy + named)."""
    ids = []
    # legacy root
    if (DATA_DIR / "sales_history.json").exists():
        ids.append("default")
    loc_root = DATA_DIR / "locations"
    if loc_root.exists():
        for d in sorted(loc_root.iterdir()):
            if d.is_dir():
                ids.append(d.name)
    return ids or ["default"]


def load_json(location_id: str, filename: str) -> list:
    p = location_dir(location_id) / filename
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_json_dict(location_id: str, filename: str) -> dict:
    p = location_dir(location_id) / filename
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(location_id: str, filename: str, data):
    d = location_dir(location_id)
    (d / filename).write_text(json.dumps(data, indent=2))


def get_location_name(location_id: str) -> str:
    """Try to get a human-readable name from users.json."""
    try:
        users = json.loads(USERS_FILE.read_text())
        for u in users.values():
            if u.get("location_id") == location_id:
                return u.get("location_name") or location_id
    except Exception:
        pass
    return location_id


def resolve_location(token_user: dict, requested_id: Optional[str]) -> Optional[str]:
    """
    Given the authenticated user and a requested location_id:
    - If user is corporate: allow any location_id (or None -> return None meaning 'all')
    - If user is store-level: ignore requested_id, return their own location_id
    Returns None to mean "all locations" (corporate only).
    """
    role = token_user.get("role", "store")
    own_id = token_user.get("location_id", "default")

    if role == "corporate":
        if not requested_id or requested_id.upper() == "ALL":
            return None  # all
        return requested_id
    else:
        return own_id  # store users always get their own data


def get_users_by_location() -> Dict[str, dict]:
    """Return dict of location_id -> user record."""
    try:
        users = json.loads(USERS_FILE.read_text())
        result = {}
        for u in users.values():
            lid = u.get("location_id", "default")
            result[lid] = u
        return result
    except Exception:
        return {}
