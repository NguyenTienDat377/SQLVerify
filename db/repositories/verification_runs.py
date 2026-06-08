"""
db/repositories/verification_runs.py

Data access layer for the verification_runs table.

Usage:
    from db.repositories.verification_runs import save_run, get_recent_runs

    run_id = await save_run(result, ddl_sql, sql_v1, sql_v2, dialect, duration_ms)
    runs   = await get_recent_runs(limit=20)
"""

from __future__ import annotations

import time
from typing import Optional
from uuid import UUID

from loguru import logger

from db.client import get_client
from core.models import VerificationResult


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def save_run(
    result: VerificationResult,
    ddl_sql: str,
    sql_v1: str,
    sql_v2: str,
    dialect: str,
    duration_ms: int,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Persist a verification run to Supabase.

    Args:
        result:      VerificationResult from check_equivalence().
        ddl_sql:     DDL schema SQL text.
        sql_v1:      Original query SQL text.
        sql_v2:      AI-generated query SQL text.
        dialect:     SQL dialect string.
        duration_ms: How long the verification took in milliseconds.
        user_id:     UUID of the authenticated user, or None for anonymous.

    Returns:
        UUID of the inserted row, or None on failure.
    """
    row = {
        "dialect":            dialect,
        "ddl_sql":            ddl_sql,
        "sql_v1":             sql_v1,
        "sql_v2":             sql_v2,
        "status":             result.status,
        "divergence_reason":  result.divergence_reason,
        "counterexample_db":  result.counterexample_db,
        "query_v1_output":    result.query_v1_output,
        "query_v2_output":    result.query_v2_output,
        "error_message":      result.error_message,
        "explanation":        result.explanation,
        "duration_ms":        duration_ms,
        "user_id":            user_id,
    }

    try:
        client = get_client()
        response = client.table("verification_runs").insert(row).execute()
        return response.data[0]["id"] if response.data else None
    except Exception as e:
        # Never let DB failure break the verification response
        logger.error("Failed to save verification run: {err}", err=e)
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_recent_runs(limit: int = 20, user_id: Optional[str] = None) -> list[dict]:
    """
    Fetch the most recent verification runs for the history panel.

    Returns a lightweight list — no full SQL text, just display fields.
    Pass user_id to scope results to a single user (required once RLS is active).
    """
    try:
        client = get_client()
        query = (
            client.table("verification_runs")
            .select("id, created_at, dialect, status, divergence_reason, duration_ms")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if user_id:
            query = query.eq("user_id", user_id)
        response = query.execute()
        return response.data or []
    except Exception as e:
        logger.error("Failed to fetch recent runs: {err}", err=e)
        return []


async def get_run_by_id(run_id: str) -> Optional[dict]:
    """
    Fetch a single verification run by UUID — full detail for result replay.
    """
    try:
        client = get_client()
        response = (
            client.table("verification_runs")
            .select("*")
            .eq("id", run_id)
            .single()
            .execute()
        )
        return response.data
    except Exception as e:
        logger.error("Failed to fetch run {run_id}: {err}", run_id=run_id, err=e)
        return None


async def update_explanation(run_id: str, explanation: str) -> bool:
    """Persist a generated explanation for an existing run."""
    try:
        client = get_client()
        client.table("verification_runs").update({"explanation": explanation}).eq("id", run_id).execute()
        return True
    except Exception as e:
        logger.error("Failed to update explanation for {run_id}: {err}", run_id=run_id, err=e)
        return False