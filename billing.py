"""
Billing, usage metering, and affiliate commissions for MCP SaaS.

This module is designed to be dropped into any MCP server template.
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class UsageRecord:
    user_id: str
    api_key: str
    tool_name: str
    timestamp: str
    duration_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class UserUsage:
    user_id: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    calls_by_tool: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    period_start: str = ""
    period_end: str = ""
    stripe_customer_id: str = ""
    stripe_subscription_id: str = ""
    tier: str = "free"


@dataclass
class APIKey:
    key: str
    user_id: str
    name: str = ""
    created_at: str = ""
    last_used: str = ""
    is_active: bool = True
    tier: str = "free"
    rate_limit_rpm: int = 60
    stripe_customer_id: str = ""
    affiliate_code: str = ""


@dataclass
class AffiliatePartner:
    code: str
    partner_name: str
    payout_email: str
    commission_rate: float = 0.20
    created_at: str = ""
    is_active: bool = True
    total_referred_calls: int = 0
    total_referred_revenue_usd: float = 0.0
    pending_commission_usd: float = 0.0
    paid_commission_usd: float = 0.0


class BillingConfig:
    def __init__(self):
        self.enabled = os.environ.get("BILLING_ENABLED", "false").lower() == "true"
        self.stripe_secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
        self.stripe_price_id = os.environ.get("STRIPE_PRICE_ID", "")
        self.stripe_webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        self.free_tier_calls = int(os.environ.get("FREE_TIER_CALLS", "100"))
        self.rate_limit_rpm = int(os.environ.get("RATE_LIMIT_RPM", "60"))
        self.usage_log_dir = os.environ.get("USAGE_LOG_DIR", "/data/usage")

        self.affiliate_enabled = os.environ.get("AFFILIATE_ENABLED", "true").lower() == "true"
        self.default_affiliate_rate = float(os.environ.get("AFFILIATE_DEFAULT_RATE", "0.20"))
        self.affiliate_payout_threshold = float(os.environ.get("AFFILIATE_PAYOUT_THRESHOLD", "100.00"))

        # Replace with your own production pricing model.
        self.tool_prices = {
            "echo": 0.0005,
            "hello_world": 0.0005,
            "get_status": 0.0,
        }


class UsageTracker:
    def __init__(self, config: BillingConfig = None):
        self.config = config or BillingConfig()
        self._stripe = None

        self._api_keys: Dict[str, APIKey] = {}
        self._usage: Dict[str, UserUsage] = {}
        self._records: List[UsageRecord] = []
        self._rate_windows: Dict[str, List[float]] = defaultdict(list)
        self._affiliates: Dict[str, AffiliatePartner] = {}

        if self.config.enabled and self.config.stripe_secret_key:
            self._init_stripe()

        self._load_state()

    def _init_stripe(self):
        try:
            import stripe

            stripe.api_key = self.config.stripe_secret_key
            self._stripe = stripe
        except ImportError:
            print("[Billing] stripe package not installed. Run: pip install stripe")
            self._stripe = None

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------
    def create_api_key(
        self,
        user_id: str,
        name: str = "",
        tier: str = "free",
        stripe_customer_id: str = "",
        affiliate_code: str = "",
    ) -> APIKey:
        import secrets

        normalized_affiliate_code = affiliate_code.strip()
        if normalized_affiliate_code and not self.get_affiliate(normalized_affiliate_code):
            normalized_affiliate_code = ""

        key = f"mcp_{secrets.token_urlsafe(32)}"
        api_key = APIKey(
            key=key,
            user_id=user_id,
            name=name or f"Key for {user_id}",
            created_at=datetime.utcnow().isoformat(),
            tier=tier,
            rate_limit_rpm=self.config.rate_limit_rpm,
            stripe_customer_id=stripe_customer_id,
            affiliate_code=normalized_affiliate_code,
        )
        self._api_keys[key] = api_key
        self._save_state()
        return api_key

    def validate_api_key(self, key: str) -> Optional[APIKey]:
        api_key = self._api_keys.get(key)
        if api_key and api_key.is_active:
            api_key.last_used = datetime.utcnow().isoformat()
            return api_key
        return None

    def revoke_api_key(self, key: str) -> bool:
        if key in self._api_keys:
            self._api_keys[key].is_active = False
            self._save_state()
            return True
        return False

    # ------------------------------------------------------------------
    # Affiliates
    # ------------------------------------------------------------------
    def create_affiliate(
        self,
        partner_name: str,
        payout_email: str,
        commission_rate: Optional[float] = None,
    ) -> AffiliatePartner:
        import secrets

        raw_code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")
        code = f"aff_{raw_code[:12]}"
        rate = (
            commission_rate
            if commission_rate is not None
            else self.config.default_affiliate_rate
        )
        rate = max(0.01, min(float(rate), 0.50))

        partner = AffiliatePartner(
            code=code,
            partner_name=partner_name.strip(),
            payout_email=payout_email.strip().lower(),
            commission_rate=rate,
            created_at=datetime.utcnow().isoformat(),
        )
        self._affiliates[code] = partner
        self._save_state()
        return partner

    def get_affiliate(self, code: str) -> Optional[AffiliatePartner]:
        partner = self._affiliates.get(code)
        if partner and partner.is_active:
            return partner
        return None

    def attach_affiliate_to_user(self, user_id: str, affiliate_code: str) -> int:
        if not self.get_affiliate(affiliate_code):
            return 0
        updated = 0
        for key in self._api_keys.values():
            if key.user_id == user_id and key.is_active:
                key.affiliate_code = affiliate_code
                updated += 1
        if updated:
            self._save_state()
        return updated

    def get_affiliate_dashboard(self, code: str) -> Optional[Dict[str, Any]]:
        partner = self.get_affiliate(code)
        if not partner:
            return None
        return {
            "code": partner.code,
            "partner_name": partner.partner_name,
            "payout_email": partner.payout_email,
            "commission_rate": partner.commission_rate,
            "totals": {
                "referred_calls": partner.total_referred_calls,
                "referred_revenue_usd": round(partner.total_referred_revenue_usd, 4),
                "pending_commission_usd": round(partner.pending_commission_usd, 4),
                "paid_commission_usd": round(partner.paid_commission_usd, 4),
            },
            "payout_threshold_usd": self.config.affiliate_payout_threshold,
            "eligible_for_payout": partner.pending_commission_usd >= self.config.affiliate_payout_threshold,
        }

    # ------------------------------------------------------------------
    # Limits and usage
    # ------------------------------------------------------------------
    def check_rate_limit(self, api_key: APIKey) -> tuple[bool, int]:
        now = time.time()
        window_start = now - 60
        key = api_key.key
        self._rate_windows[key] = [t for t in self._rate_windows[key] if t > window_start]
        current_count = len(self._rate_windows[key])
        limit = api_key.rate_limit_rpm
        if current_count >= limit:
            return False, 0
        self._rate_windows[key].append(now)
        return True, limit - current_count - 1

    def get_or_create_usage(self, user_id: str) -> UserUsage:
        if user_id not in self._usage:
            now = datetime.utcnow()
            self._usage[user_id] = UserUsage(
                user_id=user_id,
                period_start=now.replace(day=1).isoformat(),
                period_end=(now.replace(day=1) + timedelta(days=32)).replace(day=1).isoformat(),
            )
        return self._usage[user_id]

    def record_usage(
        self,
        api_key: APIKey,
        tool_name: str,
        duration_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        success: bool = True,
        error: str = "",
    ) -> UsageRecord:
        cost = self.config.tool_prices.get(tool_name, 0.001)
        record = UsageRecord(
            user_id=api_key.user_id,
            api_key=api_key.key[:12] + "...",
            tool_name=tool_name,
            timestamp=datetime.utcnow().isoformat(),
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            success=success,
            error=error,
        )
        self._records.append(record)

        usage = self.get_or_create_usage(api_key.user_id)
        usage.total_calls += 1
        usage.total_tokens += input_tokens + output_tokens
        usage.total_cost_usd += cost
        usage.calls_by_tool[tool_name] += 1

        if api_key.affiliate_code:
            partner = self.get_affiliate(api_key.affiliate_code)
            if partner:
                commission = cost * partner.commission_rate
                partner.total_referred_calls += 1
                partner.total_referred_revenue_usd += cost
                partner.pending_commission_usd += commission

        if self.config.enabled and self._stripe and api_key.stripe_customer_id:
            asyncio.get_event_loop().create_task(
                self._report_to_stripe(api_key, tool_name, cost)
            )

        self._save_state()
        return record

    def check_free_tier(self, api_key: APIKey) -> tuple[bool, int]:
        usage = self.get_or_create_usage(api_key.user_id)
        remaining = self.config.free_tier_calls - usage.total_calls
        if api_key.tier != "free":
            return True, 999999
        return remaining > 0, max(0, remaining)

    # ------------------------------------------------------------------
    # Stripe
    # ------------------------------------------------------------------
    async def _report_to_stripe(self, api_key: APIKey, tool_name: str, cost: float):
        if not self._stripe or not api_key.stripe_customer_id:
            return
        try:
            if not getattr(api_key, "_subscription_item_id", None):
                subscriptions = self._stripe.Subscription.list(
                    customer=api_key.stripe_customer_id,
                    status="active",
                    limit=1,
                )
                if subscriptions.data:
                    sub = subscriptions.data[0]
                    for item in sub["items"]["data"]:
                        if item["price"]["id"] == self.config.stripe_price_id:
                            api_key._subscription_item_id = item["id"]
                            break

            if getattr(api_key, "_subscription_item_id", None):
                quantity = max(1, int(cost * 100))
                self._stripe.SubscriptionItem.create_usage_record(
                    api_key._subscription_item_id,
                    quantity=quantity,
                    timestamp=int(time.time()),
                    action="increment",
                )
        except Exception as e:
            print(f"[Billing] Stripe usage report error: {e}")

    async def create_checkout_session(
        self,
        user_id: str,
        tier: str = "pro",
        success_url: str = "",
        cancel_url: str = "",
    ) -> Optional[str]:
        if not self._stripe:
            return None
        try:
            session = self._stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": self.config.stripe_price_id}],
                success_url=success_url or "https://your-app.com/success",
                cancel_url=cancel_url or "https://your-app.com/cancel",
                metadata={"user_id": user_id, "tier": tier},
            )
            return session.url
        except Exception as e:
            print(f"[Billing] Checkout session error: {e}")
            return None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def get_usage_summary(self, user_id: str) -> Dict[str, Any]:
        usage = self.get_or_create_usage(user_id)
        return {
            "user_id": user_id,
            "period": {"start": usage.period_start, "end": usage.period_end},
            "total_calls": usage.total_calls,
            "total_tokens": usage.total_tokens,
            "total_cost_usd": round(usage.total_cost_usd, 4),
            "calls_by_tool": dict(usage.calls_by_tool),
            "tier": usage.tier,
        }

    def get_global_metrics(self) -> Dict[str, Any]:
        total_users = len(self._usage)
        total_calls = sum(u.total_calls for u in self._usage.values())
        total_revenue = sum(u.total_cost_usd for u in self._usage.values())
        tool_totals: Dict[str, int] = defaultdict(int)
        for usage in self._usage.values():
            for tool, count in usage.calls_by_tool.items():
                tool_totals[tool] += count

        pending_affiliate = sum(a.pending_commission_usd for a in self._affiliates.values())
        affiliate_revenue = sum(a.total_referred_revenue_usd for a in self._affiliates.values())
        return {
            "total_users": total_users,
            "total_api_keys": len(self._api_keys),
            "total_calls": total_calls,
            "total_revenue_usd": round(total_revenue, 2),
            "affiliate": {
                "partners": len(self._affiliates),
                "referred_revenue_usd": round(affiliate_revenue, 2),
                "pending_commission_usd": round(pending_affiliate, 2),
            },
            "calls_by_tool": dict(sorted(tool_totals.items(), key=lambda x: -x[1])),
            "active_period": datetime.utcnow().strftime("%Y-%m"),
        }

    def get_recent_activity(self, user_id: str = None, limit: int = 50) -> List[Dict]:
        records = self._records
        if user_id:
            records = [r for r in records if r.user_id == user_id]
        return [asdict(r) for r in records[-limit:]]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save_state(self):
        state_dir = Path(self.config.usage_log_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "api_keys": {k: asdict(v) for k, v in self._api_keys.items()},
            "usage": {
                k: {**asdict(v), "calls_by_tool": dict(v.calls_by_tool)}
                for k, v in self._usage.items()
            },
            "affiliates": {k: asdict(v) for k, v in self._affiliates.items()},
        }
        try:
            (state_dir / "billing_state.json").write_text(
                json.dumps(state, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_state(self):
        state_file = Path(self.config.usage_log_dir) / "billing_state.json"
        if not state_file.exists():
            return
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            for key, data in state.get("api_keys", {}).items():
                self._api_keys[key] = APIKey(**{
                    k: v for k, v in data.items() if k in APIKey.__dataclass_fields__
                })
            for user_id, data in state.get("usage", {}).items():
                calls_by_tool = data.pop("calls_by_tool", {})
                usage = UserUsage(**{
                    k: v for k, v in data.items() if k in UserUsage.__dataclass_fields__
                })
                usage.calls_by_tool = defaultdict(int, calls_by_tool)
                self._usage[user_id] = usage
            for code, data in state.get("affiliates", {}).items():
                self._affiliates[code] = AffiliatePartner(**{
                    k: v for k, v in data.items() if k in AffiliatePartner.__dataclass_fields__
                })
        except Exception as e:
            print(f"[Billing] Failed to load state: {e}")


def create_billing_middleware(tracker: UsageTracker):
    async def middleware(
        tool_name: str,
        arguments: Dict[str, Any],
        api_key_str: str,
        handler,
    ) -> Dict[str, Any]:
        if tracker.config.enabled:
            api_key = tracker.validate_api_key(api_key_str)
            if not api_key:
                return {"error": {"code": -32000, "message": "Invalid API key"}}
            allowed, _remaining = tracker.check_rate_limit(api_key)
            if not allowed:
                return {"error": {"code": -32001, "message": "Rate limit exceeded"}}
            within_free, _remaining_free = tracker.check_free_tier(api_key)
            if not within_free:
                return {
                    "error": {
                        "code": -32002,
                        "message": (
                            f"Free tier limit ({tracker.config.free_tier_calls} calls/month) exceeded. "
                            "Upgrade at /billing/checkout"
                        ),
                    }
                }
        else:
            api_key = APIKey(key="anonymous", user_id="anonymous")

        start = time.time()
        success = True
        error_msg = ""
        try:
            result = await handler(**arguments)
        except Exception as e:
            success = False
            error_msg = str(e)
            result = f"Error: {error_msg}"

        duration_ms = (time.time() - start) * 1000
        tracker.record_usage(
            api_key=api_key,
            tool_name=tool_name,
            duration_ms=duration_ms,
            success=success,
            error=error_msg,
        )
        return {"content": [{"type": "text", "text": result}]}

    return middleware


def add_billing_routes(app, tracker: UsageTracker):
    from aiohttp import web

    async def handle_create_key(request):
        data = await request.json()
        user_id = data.get("user_id") or data.get("email")
        if not user_id:
            return web.json_response({"error": "user_id or email required"}, status=400)
        api_key = tracker.create_api_key(
            user_id=user_id,
            name=data.get("name", ""),
            tier=data.get("tier", "free"),
            stripe_customer_id=data.get("stripe_customer_id", ""),
            affiliate_code=data.get("affiliate_code", ""),
        )
        return web.json_response({
            "api_key": api_key.key,
            "apiKey": api_key.key,
            "user_id": api_key.user_id,
            "tier": api_key.tier,
            "affiliate_code": api_key.affiliate_code,
            "created_at": api_key.created_at,
        })

    async def handle_usage(request):
        api_key_str = request.query.get("api_key", "")
        api_key = tracker.validate_api_key(api_key_str)
        if not api_key:
            return web.json_response({"error": "Invalid API key"}, status=401)
        return web.json_response(tracker.get_usage_summary(api_key.user_id))

    async def handle_metrics(request):
        admin_key = request.query.get("admin_key", "")
        expected = os.environ.get("BILLING_ADMIN_KEY", "")
        if expected and admin_key != expected:
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response(tracker.get_global_metrics())

    async def handle_activity(request):
        api_key_str = request.query.get("api_key", "")
        api_key = tracker.validate_api_key(api_key_str)
        if not api_key:
            return web.json_response({"error": "Invalid API key"}, status=401)
        limit = int(request.query.get("limit", "50"))
        return web.json_response({"activity": tracker.get_recent_activity(api_key.user_id, limit)})

    async def handle_checkout(request):
        data = await request.json()
        user_id = data.get("user_id") or data.get("email")
        if not user_id:
            return web.json_response({"error": "user_id or email required"}, status=400)
        url = await tracker.create_checkout_session(
            user_id=user_id,
            tier=data.get("tier", data.get("plan", "pro")),
            success_url=data.get("success_url", ""),
            cancel_url=data.get("cancel_url", ""),
        )
        if url:
            return web.json_response({"checkout_url": url, "url": url})
        return web.json_response({"error": "Stripe not configured"}, status=503)

    async def handle_stripe_webhook(request):
        if not tracker._stripe:
            return web.Response(status=503)
        payload = await request.read()
        sig_header = request.headers.get("Stripe-Signature", "")
        try:
            event = tracker._stripe.Webhook.construct_event(
                payload, sig_header, tracker.config.stripe_webhook_secret
            )
        except Exception:
            return web.Response(status=400)

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session.get("metadata", {}).get("user_id")
            customer_id = session.get("customer")
            if user_id and customer_id:
                for key in tracker._api_keys.values():
                    if key.user_id == user_id:
                        key.stripe_customer_id = customer_id
                        key.tier = "pro"
                        key.rate_limit_rpm = 300
                tracker._save_state()
        elif event["type"] == "customer.subscription.deleted":
            customer_id = event["data"]["object"]["customer"]
            for key in tracker._api_keys.values():
                if key.stripe_customer_id == customer_id:
                    key.tier = "free"
                    key.rate_limit_rpm = tracker.config.rate_limit_rpm
            tracker._save_state()

        return web.json_response({"received": True})

    async def handle_affiliate_signup(request):
        if not tracker.config.affiliate_enabled:
            return web.json_response({"error": "Affiliate program disabled"}, status=403)
        data = await request.json()
        partner_name = data.get("partner_name") or data.get("name")
        payout_email = data.get("payout_email") or data.get("email")
        if not partner_name or not payout_email:
            return web.json_response({"error": "partner_name and payout_email required"}, status=400)
        partner = tracker.create_affiliate(
            partner_name=partner_name,
            payout_email=payout_email,
            commission_rate=data.get("commission_rate"),
        )
        return web.json_response({
            "affiliate_code": partner.code,
            "partner_name": partner.partner_name,
            "commission_rate": partner.commission_rate,
            "payout_email": partner.payout_email,
            "created_at": partner.created_at,
            "referral_url": f"{request.scheme}://{request.host}/?ref={partner.code}",
        })

    async def handle_affiliate_dashboard(request):
        code = request.query.get("code", "")
        if not code:
            return web.json_response({"error": "code required"}, status=400)
        dashboard = tracker.get_affiliate_dashboard(code)
        if not dashboard:
            return web.json_response({"error": "affiliate not found"}, status=404)
        return web.json_response(dashboard)

    async def handle_affiliate_attach(request):
        data = await request.json()
        user_id = data.get("user_id") or data.get("email")
        code = data.get("affiliate_code") or data.get("code")
        if not user_id or not code:
            return web.json_response({"error": "user_id/email and affiliate_code required"}, status=400)
        updated = tracker.attach_affiliate_to_user(user_id=user_id, affiliate_code=code)
        if updated == 0:
            return web.json_response(
                {"error": "No API keys updated (invalid code or no matching user)"},
                status=404,
            )
        return web.json_response({"updated_api_keys": updated, "affiliate_code": code, "user_id": user_id})

    async def handle_affiliate_offer(_request):
        return web.json_response({
            "enabled": tracker.config.affiliate_enabled,
            "default_commission_rate": tracker.config.default_affiliate_rate,
            "payout_threshold_usd": tracker.config.affiliate_payout_threshold,
        })

    app.router.add_post("/billing/keys", handle_create_key)
    app.router.add_get("/billing/usage", handle_usage)
    app.router.add_get("/billing/metrics", handle_metrics)
    app.router.add_get("/billing/activity", handle_activity)
    app.router.add_post("/billing/checkout", handle_checkout)
    app.router.add_post("/billing/webhook", handle_stripe_webhook)

    app.router.add_post("/affiliate/signup", handle_affiliate_signup)
    app.router.add_get("/affiliate/dashboard", handle_affiliate_dashboard)
    app.router.add_post("/affiliate/attach", handle_affiliate_attach)
    app.router.add_get("/affiliate/public-offer", handle_affiliate_offer)
