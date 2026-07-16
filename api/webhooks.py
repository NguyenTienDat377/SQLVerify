"""
api/webhooks.py

POST /api/webhooks/lemonsqueezy — receives Lemon Squeezy subscription events.

Handles:
  - subscription_created   → upsert as active
  - subscription_updated   → upsert with new status/period
  - subscription_cancelled → mark as cancelled
  - subscription_expired   → mark as expired
  - subscription_resumed   → mark as active

Signature verification:
  Lemon Squeezy sends X-Signature (HMAC SHA-256 of raw body).
  We verify before processing — reject 401 if invalid.

Environment variables required:
  LEMONSQUEEZY_WEBHOOK_SECRET  — from Lemon Squeezy → Settings → Webhooks
  LS_INDIVIDUAL_VARIANT_ID     — variant ID for Individual plan ($9/mo)
  LS_TEAM_VARIANT_ID           — variant ID for Team plan ($49/mo)
"""

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core.analytics import capture_subscription_event
from db.repositories.subscriptions import upsert_subscription

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Tier mapping — fill LS_*_VARIANT_ID in .env after creating products
# ---------------------------------------------------------------------------

def _get_variant_tier_map() -> dict[str, str]:
    """
    Build variant ID → tier mapping from env vars.
    Skips empty strings so unset vars don't create a catch-all "" key.
    """
    mapping: dict[str, str] = {}
    ind  = os.getenv("LS_INDIVIDUAL_VARIANT_ID", "")
    team = os.getenv("LS_TEAM_VARIANT_ID", "")
    if ind:
        mapping[ind]  = "individual"
    if team:
        mapping[team] = "team"
    return mapping


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature: str) -> bool:
    """
    Verify Lemon Squeezy webhook signature.
    HMAC SHA-256 of raw request body, compared to X-Signature header.
    """
    secret = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    if not secret:
        print("[webhooks] WARNING: LEMONSQUEEZY_WEBHOOK_SECRET not set")
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

HANDLED_EVENTS = {
    "subscription_created",
    "subscription_updated",
    "subscription_cancelled",
    "subscription_expired",
    "subscription_resumed",
    "subscription_payment_failed",
}

# Map Lemon Squeezy subscription status → our status
EVENT_STATUS_OVERRIDE = {
    "subscription_cancelled":      "cancelled",
    "subscription_expired":        "expired",
    "subscription_resumed":        "active",
    "subscription_payment_failed": "past_due",
}


@router.post("/lemonsqueezy")
async def lemonsqueezy_webhook(
    request: Request,
    x_signature: str = Header(..., alias="X-Signature"),
):
    """
    Receive and process Lemon Squeezy subscription lifecycle events.

    Always returns 200 for known events — Lemon Squeezy retries on non-2xx.
    Returns 401 for invalid signatures, 413 for oversized payloads.
    """
    # Guard against oversized payloads before reading body into memory
    content_length = int(request.headers.get("content-length", 0))
    if content_length > 1_048_576:  # 1 MB cap — real LS payloads are ~2–5 KB
        raise HTTPException(status_code=413, detail="Payload too large")

    body = await request.body()

    if not _verify_signature(body, x_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_name = payload.get("meta", {}).get("event_name", "")

    # Acknowledge unhandled events without error
    if event_name not in HANDLED_EVENTS:
        return JSONResponse({"received": True, "handled": False, "event": event_name})

    data       = payload.get("data", {})
    attributes = data.get("attributes", {})
    # user_id is round-tripped through LS checkout custom data (set by
    # /billing/checkout). Present for in-app purchases; absent otherwise.
    custom_data = payload.get("meta", {}).get("custom_data", {}) or {}
    user_id     = custom_data.get("user_id") or None

    subscription_id = str(data.get("id", ""))
    customer_id     = str(attributes.get("customer_id", ""))
    customer_email  = attributes.get("user_email", "")
    order_id        = str(attributes.get("order_id", ""))
    product_id      = str(attributes.get("product_id", ""))
    variant_id      = str(attributes.get("variant_id", ""))
    period_end      = attributes.get("renews_at")

    # Status: prefer event-level override, else use attributes.status
    status = EVENT_STATUS_OVERRIDE.get(event_name, attributes.get("status", "active"))

    # Map variant ID → tier (falls back to "individual" if unknown)
    tier = _get_variant_tier_map().get(variant_id, "individual")

    await upsert_subscription(
        customer_id=customer_id,
        customer_email=customer_email,
        subscription_id=subscription_id,
        order_id=order_id,
        product_id=product_id,
        variant_id=variant_id,
        status=status,
        tier=tier,
        current_period_end=period_end,
        user_id=user_id,
    )

    capture_subscription_event(
        user_id=user_id,
        event_type=event_name,
        tier=tier,
        status=status,
    )
    print(f"[webhooks] {event_name} → {customer_email} ({tier}, {status}, user={user_id})")
    return JSONResponse({"received": True, "handled": True, "event": event_name})