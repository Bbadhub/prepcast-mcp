"""
PrepCast - Authentication

Simple email/password auth with SHA-256+salt hashing.
Users are stored in Postgres (when DATABASE_URL is set) or users.json fallback.
On signup: creates a user record + issues a bearer token.
On login: validates password, returns bearer token.

The bearer token IS the MCP API key (mcp_xxx format) -- users paste it
directly into their Claude connector or Authorization header.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Dict, Optional

from aiohttp import web
from store import slugify, load_users, save_users, upsert_user, get_user_by_api_key

TRIAL_DAYS = 14


def _hash_password(password: str) -> str:
    """SHA-256 + salt. Simple but sufficient for this use case."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


def _trial_expires_at(created_at: str) -> str:
    try:
        created = datetime.fromisoformat(created_at)
    except Exception:
        created = datetime.utcnow()
    return (created + timedelta(days=TRIAL_DAYS)).isoformat()


def _is_trial_active(user: Dict) -> bool:
    expires = user.get("trial_expires_at", "")
    if not expires:
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(expires)
    except Exception:
        return False


def _days_left_in_trial(user: Dict) -> int:
    expires = user.get("trial_expires_at", "")
    if not expires:
        return 0
    try:
        delta = datetime.fromisoformat(expires) - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return 0


def add_auth_routes(app: web.Application, usage_tracker):
    """Register /auth/* routes on the aiohttp app."""

    async def handle_signup(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip()
        location_name = (data.get("location_name") or "").strip()
        role = (data.get("role") or "store").strip().lower()
        # Only allow corporate role if a secret is provided
        corporate_secret = os.environ.get("CORPORATE_SIGNUP_SECRET", "")
        if role == "corporate" and data.get("corporate_secret", "") != corporate_secret:
            role = "store"

        if not email or "@" not in email:
            return web.json_response({"error": "Valid email required"}, status=400)
        if len(password) < 6:
            return web.json_response({"error": "Password must be at least 6 characters"}, status=400)

        users = load_users()
        if email in users:
            return web.json_response({"error": "Account already exists"}, status=409)

        location_id = slugify(location_name) if location_name else slugify(email.split("@")[0])
        created_at = datetime.utcnow().isoformat()
        trial_expires_at = _trial_expires_at(created_at)

        # Create API key via billing system
        api_key_obj = usage_tracker.create_api_key(
            user_id=email,
            name=f"{name or email} - {location_name or 'Default Location'}",
            tier="trial",
        )

        user = {
            "email": email,
            "name": name,
            "location_name": location_name,
            "location_id": location_id,
            "role": role,
            "password_hash": _hash_password(password),
            "api_key": api_key_obj.key,
            "created_at": created_at,
            "trial_expires_at": trial_expires_at,
            "tier": "trial",
            "is_active": True,
        }
        upsert_user(email, user)

        return web.json_response({
            "ok": True,
            "email": email,
            "name": name,
            "location_id": location_id,
            "location_name": location_name,
            "role": role,
            "api_key": api_key_obj.key,
            "bearer_token": api_key_obj.key,
            "trial_days_remaining": TRIAL_DAYS,
            "trial_expires_at": trial_expires_at,
            "message": (
                f"Welcome to PrepCast! Your 14-day free trial starts now. "
                f"Your bearer token is: {api_key_obj.key}"
            ),
            "connect_instructions": (
                "To connect to Claude: go to claude.ai -> Customize -> Connectors -> "
                "Add Custom -> paste your PrepCast SSE URL. "
                "Add your bearer token as: Authorization: Bearer <your_token>"
            ),
        })

    async def handle_login(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""

        if not email or not password:
            return web.json_response({"error": "email and password required"}, status=400)

        users = load_users()
        user = users.get(email)

        if not user or not _verify_password(password, user.get("password_hash", "")):
            return web.json_response({"error": "Invalid email or password"}, status=401)

        if not user.get("is_active"):
            return web.json_response({"error": "Account is deactivated"}, status=403)

        trial_active = _is_trial_active(user)
        days_left = _days_left_in_trial(user)

        return web.json_response({
            "ok": True,
            "email": user["email"],
            "name": user.get("name", ""),
            "api_key": user["api_key"],
            "bearer_token": user["api_key"],
            "tier": user.get("tier", "trial"),
            "role": user.get("role", "store"),
            "location_id": user.get("location_id", "default"),
            "location_name": user.get("location_name", ""),
            "trial_active": trial_active,
            "trial_days_remaining": days_left,
            "trial_expires_at": user.get("trial_expires_at", ""),
        })

    async def handle_me(request: web.Request) -> web.Response:
        api_key_str = request.headers.get("Authorization", "")
        if api_key_str.startswith("Bearer "):
            api_key_str = api_key_str[7:]
        if not api_key_str:
            api_key_str = request.headers.get("X-API-Key", "")

        user = get_user_by_api_key(api_key_str)
        if not user:
            return web.json_response({"error": "Invalid token"}, status=401)

        trial_active = _is_trial_active(user)
        days_left = _days_left_in_trial(user)

        return web.json_response({
            "email": user["email"],
            "name": user.get("name", ""),
            "location_id": user.get("location_id", "default"),
            "location_name": user.get("location_name", ""),
            "role": user.get("role", "store"),
            "tier": user.get("tier", "trial"),
            "trial_active": trial_active,
            "trial_days_remaining": days_left,
            "trial_expires_at": user.get("trial_expires_at", ""),
            "created_at": user.get("created_at", ""),
        })

    async def handle_users_list(request: web.Request) -> web.Response:
        """Admin endpoint: list all users."""
        admin_key = request.query.get("admin_key", "")
        expected = os.environ.get("BILLING_ADMIN_KEY", "")
        if expected and admin_key != expected:
            return web.json_response({"error": "Unauthorized"}, status=401)

        users = load_users()
        result = []
        for u in users.values():
            result.append({
                "email": u["email"],
                "name": u.get("name", ""),
                "location_name": u.get("location_name", ""),
                "tier": u.get("tier", "trial"),
                "trial_active": _is_trial_active(u),
                "trial_days_remaining": _days_left_in_trial(u),
                "created_at": u.get("created_at", ""),
                "is_active": u.get("is_active", True),
            })
        return web.json_response({"users": result, "total": len(result)})

    app.router.add_post("/auth/signup", handle_signup)
    app.router.add_post("/auth/login", handle_login)
    app.router.add_get("/auth/me", handle_me)
    app.router.add_get("/auth/users", handle_users_list)
