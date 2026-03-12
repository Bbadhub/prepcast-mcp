"""
PrepCast - Multi-Store Data Layer

When DATABASE_URL is set, all persistence goes to Postgres (survives restarts).
When DATABASE_URL is not set, falls back to flat-file JSON in PREPCAST_DATA_DIR.

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


# ---------------------------------------------------------------------------
# Users — flat file helpers (used by _FileBackend in db.py)
# ---------------------------------------------------------------------------

def _load_users_file() -> Dict:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {}


def _save_users_file(users: Dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))


# ---------------------------------------------------------------------------
# Storage — routes to Postgres when DATABASE_URL is set
# ---------------------------------------------------------------------------

def _use_pg() -> bool:
    return bool(os.environ.get("DATABASE_URL", ""))


def list_location_ids() -> List[str]:
    if _use_pg():
        from db import pg_list_location_ids
        return pg_list_location_ids()
    # flat-file
    ids = []
    if (DATA_DIR / "sales_history.json").exists():
        ids.append("default")
    loc_root = DATA_DIR / "locations"
    if loc_root.exists():
        for d in sorted(loc_root.iterdir()):
            if d.is_dir():
                ids.append(d.name)
    return ids or ["default"]


def load_json(location_id: str, filename: str) -> list:
    if _use_pg():
        from db import pg_load_list
        return pg_load_list(location_id, filename)
    p = location_dir(location_id) / filename
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_json_dict(location_id: str, filename: str) -> dict:
    if _use_pg():
        from db import pg_load_dict
        return pg_load_dict(location_id, filename)
    p = location_dir(location_id) / filename
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(location_id: str, filename: str, data):
    if _use_pg():
        from db import pg_save
        pg_save(location_id, filename, data)
        return
    d = location_dir(location_id)
    (d / filename).write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Users — routes to Postgres when available
# ---------------------------------------------------------------------------

def load_users() -> Dict[str, dict]:
    if _use_pg():
        from db import pg_load_users
        return pg_load_users()
    return _load_users_file()


def save_users(users: Dict[str, dict]):
    if _use_pg():
        from db import pg_save_users
        pg_save_users(users)
        return
    _save_users_file(users)


def upsert_user(email: str, user: dict):
    if _use_pg():
        from db import pg_upsert_user
        pg_upsert_user(email, user)
        return
    users = _load_users_file()
    users[email] = user
    _save_users_file(users)


def get_user_by_api_key(api_key: str) -> Optional[dict]:
    if _use_pg():
        from db import pg_get_user_by_api_key
        return pg_get_user_by_api_key(api_key)
    users = _load_users_file()
    return next((u for u in users.values() if u.get("api_key") == api_key), None)


def get_location_name(location_id: str) -> str:
    """Try to get a human-readable name from users."""
    try:
        users = load_users()
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
        users = load_users()
        result = {}
        for u in users.values():
            lid = u.get("location_id", "default")
            result[lid] = u
        return result
    except Exception:
        return {}
