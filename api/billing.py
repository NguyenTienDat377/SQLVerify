"""
api/billing.py

Lemon Squeezy billing: in-app checkout redirect + customer portal.

Session-protected (uses request.state.user_id / user_email injected by
JWTMiddleware). Lemon Squeezy is the Merchant of Record — it hosts checkout and
the customer portal — so we only build/redirect to the right URLs:

  GET /billing/checkout?plan=individual|team
      → redirect to the LS hosted checkout, carrying the buyer's user_id as
        custom data so the webhook can link the subscription to this account.
  GET /billing/portal
      → fetch a fresh (signed, short-lived) customer-portal URL from the LS API
        and redirect to it.
"""

import os
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from db.repositories.subscriptions import get_active_subscription_by_user

router = APIRouter(prefix="/billing", tags=["billing"])

# plan slug → env var holding that plan's LS buy-link
_PLAN_CHECKOUT_ENV = {
    "individual": "LS_INDIVIDUAL_CHECKOUT_URL",
    "team":       "LS_TEAM_CHECKOUT_URL",
}


@router.get("/checkout")
async def checkout(request: Request, plan: str = "individual"):
    env_var = _PLAN_CHECKOUT_ENV.get(plan)
    if env_var is None:
        raise HTTPException(status_code=400, detail=f"Unknown plan '{plan}'.")

    base_url = os.getenv(env_var)
    if not base_url:
        logger.error("Checkout URL not configured: {env}", env=env_var)
        return RedirectResponse(url="/pricing", status_code=303)

    user_id = getattr(request.state, "user_id", None)
    email = getattr(request.state, "user_email", None)

    # Round-trip user_id through LS custom data so the webhook links the
    # subscription back to this account; prefill email for a smoother checkout.
    params = {"checkout[custom][user_id]": user_id or ""}
    if email:
        params["checkout[email]"] = email

    sep = "&" if "?" in base_url else "?"
    return RedirectResponse(url=f"{base_url}{sep}{urlencode(params)}", status_code=303)


@router.get("/portal")
async def portal(request: Request):
    user_id = getattr(request.state, "user_id", None)
    sub = await get_active_subscription_by_user(user_id)
    if not sub or not sub.get("subscription_id"):
        return RedirectResponse(url="/pricing", status_code=303)

    api_key = os.getenv("LEMONSQUEEZY_API_KEY", "")
    if not api_key:
        logger.error("LEMONSQUEEZY_API_KEY not set — cannot open billing portal")
        return RedirectResponse(url="/pricing", status_code=303)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.lemonsqueezy.com/v1/subscriptions/{sub['subscription_id']}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/vnd.api+json",
                },
            )
        if resp.status_code >= 400:
            logger.warning("LS portal lookup failed {code}: {body}",
                           code=resp.status_code, body=resp.text)
            return RedirectResponse(url="/pricing", status_code=303)
        portal_url = (
            resp.json().get("data", {}).get("attributes", {})
            .get("urls", {}).get("customer_portal")
        )
    except httpx.HTTPError as e:
        logger.warning("LS portal request error: {err}", err=e)
        return RedirectResponse(url="/pricing", status_code=303)

    if not portal_url:
        return RedirectResponse(url="/pricing", status_code=303)
    return RedirectResponse(url=portal_url, status_code=303)
