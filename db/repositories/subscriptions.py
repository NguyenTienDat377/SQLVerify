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
) -> Optional[str]:
    """
    Insert or update a subscription row keyed on subscription_id.

    Called on every Lemon Squeezy subscription_* webhook event.
    Uses upsert so the same subscription_id is never duplicated.

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