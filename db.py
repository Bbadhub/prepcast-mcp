"""
PrepCast - Postgres persistence layer (sync).

When DATABASE_URL is set, all data is stored in Postgres (survives restarts).
When DATABASE_URL is not set, falls back to flat-file JSON in PREPCAST_DATA_DIR.

This module exposes the same interface as store.py but routes to Postgres
when available. All existing tools continue to work without changes.

Tables:
  prepcast_users      - user accounts (email -> jsonb)
  prepcast_store_data - per-location JSON blobs (location_id + filename -> jsonb)
"""

import json
import os
from typing import Any, Dict, List, Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg2
        import psycopg2.extras
        _conn = psycopg2.connect(DATABASE_URL)
        _conn.autocommit = True
        _init_schema(_conn)
    return _conn


def _init_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prepcast_users (
                email       TEXT PRIMARY KEY,
                data        JSONB NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prepcast_store_data (
                location_id TEXT NOT NULL,
                filename    TEXT NOT NULL,
                data        JSONB NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (location_id, filename)
            )
        """)


# ---------------------------------------------------------------------------
# Store data (sales_history, forecast_log, etc.)
# ---------------------------------------------------------------------------

def pg_load(location_id: str, filename: str, default=None) -> Any:
    """Load a JSON blob from Postgres. Returns default if not found."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM prepcast_store_data WHERE location_id=%s AND filename=%s",
                (location_id, filename)
            )
            row = cur.fetchone()
            if row is None:
                return default if default is not None else []
            return row[0]  # psycopg2 auto-parses jsonb
    except Exception as e:
        print(f"[db] pg_load error {location_id}/{filename}: {e}")
        return default if default is not None else []


def pg_load_dict(location_id: str, filename: str) -> dict:
    val = pg_load(location_id, filename, default={})
    return val if isinstance(val, dict) else {}


def pg_load_list(location_id: str, filename: str) -> list:
    val = pg_load(location_id, filename, default=[])
    return val if isinstance(val, list) else []


def pg_save(location_id: str, filename: str, data: Any):
    """Upsert a JSON blob into Postgres."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prepcast_store_data (location_id, filename, data, updated_at)
                VALUES (%s, %s, %s::jsonb, NOW())
                ON CONFLICT (location_id, filename)
                DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()
            """, (location_id, filename, json.dumps(data)))
    except Exception as e:
        print(f"[db] pg_save error {location_id}/{filename}: {e}")
        raise


def pg_list_location_ids() -> List[str]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT location_id FROM prepcast_store_data ORDER BY location_id"
            )
            ids = [row[0] for row in cur.fetchall()]
            return ids or ["default"]
    except Exception as e:
        print(f"[db] pg_list_location_ids error: {e}")
        return ["default"]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def pg_load_users() -> Dict[str, dict]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT email, data FROM prepcast_users")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        print(f"[db] pg_load_users error: {e}")
        return {}


def pg_save_users(users: Dict[str, dict]):
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            for email, user in users.items():
                cur.execute("""
                    INSERT INTO prepcast_users (email, data, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (email)
                    DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()
                """, (email, json.dumps(user)))
    except Exception as e:
        print(f"[db] pg_save_users error: {e}")
        raise


def pg_upsert_user(email: str, user: dict):
    """Upsert a single user record."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prepcast_users (email, data, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (email)
                DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()
            """, (email, json.dumps(user)))
    except Exception as e:
        print(f"[db] pg_upsert_user error {email}: {e}")
        raise


def pg_get_user_by_api_key(api_key: str) -> Optional[dict]:
    """Look up a user by their API key (bearer token)."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM prepcast_users WHERE data->>'api_key' = %s",
                (api_key,)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[db] pg_get_user_by_api_key error: {e}")
        return None


# ---------------------------------------------------------------------------
# Feature detection — use Postgres only when DATABASE_URL is configured
# ---------------------------------------------------------------------------

def using_postgres() -> bool:
    return bool(DATABASE_URL)
