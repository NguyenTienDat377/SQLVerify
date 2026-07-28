"""
db/repositories/subscriptions.py

Data access layer for the subscriptions table.
Stores Lemon Squeezy subscription events and maps
customer emails to their active tier.

Usage:
    from db.repositories.subscriptions import upsert_subscription, get_active_subscription

    await upsert_subscription(customer_id="123", customer_email="user@example.com", ...)
    sub = await get_active_subscription(email="user@example.com")
"""

from __future__ import annotations

from typing import Optional

from db.client import get_client


async def upsert_subscription(
    customer_id: str,
    customer_email: str,
    subscription_id: str,
    order_id: str,
    product_id: str,
    variant_id: str,
    status: str,
    tier: str,
    current_period_end: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Insert or update a subscription row keyed on subscription_id.

    Called on every Lemon Squeezy subscription_* webhook event.
    Uses upsert so the same subscription_id is never duplicated.

    Args:
        user_id: Skolem user the subscription belongs to, passed through LS
                 checkout custom data (meta.custom_data.user_id). Used to resolve
                 a user's plan/quota; may be None for purchases made outside the
                 in-app /billing/checkout flow.

    Returns:
        UUID of the row, or None on failure.
    """
    row = {
        "customer_id":        customer_id,
        "customer_email":     customer_email,
        "subscription_id":    subscription_id,
        "order_id":           order_id,
        "product_id":         product_id,
        "variant_id":         variant_id,
        "status":             status,
        "tier":               tier,
        "current_period_end": current_period_end,
        "updated_at":         "now()",
    }
    # Only set user_id when we actually have one, so a later webhook without
    # custom data (e.g. subscription_updated) doesn't blank an existing link.
    if user_id:
        row["user_id"] = user_id

    try:
        client = get_client()
        response = (
            client.table("subscriptions")
            .upsert(row, on_conflict="subscription_id")
            .execute()
        )
        return response.data[0]["id"] if response.data else None
    except Exception as e:
        print(f"[db] Failed to upsert subscription: {e}")
        return None


async def get_active_subscription(email: str) -> Optional[dict]:
    """
    Fetch the most recent active subscription for a user by email.

    Used by auth middleware to check tier access.

    Returns:
        Subscription dict with 'tier' and 'status', or None if not found.
    """
    try:
        client = get_client()
        response = (
            client.table("subscriptions")
            .select("id, tier, status, current_period_end, variant_id")
            .eq("customer_email", email)
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"[db] Failed to get subscription for {email}: {e}")
        return None


async def get_active_subscription_by_user(user_id: str) -> Optional[dict]:
    """
    Fetch the most recent active subscription for a user by user_id.

    This is the canonical plan/quota lookup — in-app checkout stamps the
    subscription with the buyer's user_id (via LS custom data), so plan state
    resolves consistently across both the session and API-key auth paths.

    Returns:
        Subscription dict with 'tier' and 'status', or None if not found.
    """
    if not user_id:
        return None
    try:
        client = get_client()
        response = (
            client.table("subscriptions")
            .select("id, tier, status, current_period_end, variant_id, subscription_id")
            .eq("user_id", user_id)
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"[db] Failed to get subscription for user {user_id}: {e}")
        return None

from datetime import datetime, timezone


async def get_new_subscriptions_today() -> int:
    """
    Count subscriptions created today (UTC) — used as a revenue-event proxy
    for the daily Discord report. Returns 0 on failure.
    """
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    try:
        client = get_client()
        response = (
            client.table("subscriptions")
            .select("id", count="exact")
            .gte("created_at", day_start.isoformat())
            .execute()
        )
        return response.count or 0
    except Exception as e:
        print(f"[db] Failed to count today's subscriptions: {e}")
        return 0