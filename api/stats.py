"""
api/stats.py

GET /api/stats/daily — internal endpoint for the daily Discord report.

Protected by a shared secret header (X-Internal-Secret), not user JWT —
this is called by a GitHub Actions cron job, not a logged-in user. The path
is listed in auth/middleware.py's _PUBLIC_EXACT so JWTMiddleware skips it;
the secret check below is what actually guards it.

Environment variables required:
  INTERNAL_STATS_SECRET  — shared secret, also set as a GitHub Actions secret
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException

from db.repositories.verification_runs import get_daily_stats
from db.repositories.subscriptions import get_new_subscriptions_today

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _verify_secret(x_internal_secret: str) -> bool:
    expected = os.getenv("INTERNAL_STATS_SECRET", "")
    if not expected:
        return False
    return x_internal_secret == expected


@router.get("/daily")
async def daily_stats(x_internal_secret: str = Header(..., alias="X-Internal-Secret")):
    if not _verify_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="Invalid internal secret")

    verifications = await get_daily_stats()
    new_subscriptions = await get_new_subscriptions_today()

    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "verifications": verifications,
        "new_subscriptions": new_subscriptions,
    }