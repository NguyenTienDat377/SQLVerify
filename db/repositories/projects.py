"""
db/repositories/projects.py

Data access layer for the projects table — per-user projects that group
verification runs (Migration #4).

Access model mirrors api_keys / verification_runs: the service-key Supabase
client bypasses RLS, so every function scopes by owner_id in code. RLS policies
exist as defense-in-depth only.

Usage:
    proj = await create_project(user_id, "Payments API", "Billing service queries")
    rows = await list_projects(user_id)
    await delete_project(user_id, project_id)
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from db.client import get_client

_NAME_MAX = 100
_DESC_MAX = 500


async def create_project(
    user_id: str, name: str, description: str | None = None
) -> Optional[dict]:
    """
    Create a project owned by user_id. Returns the inserted row, or None on
    failure (including a duplicate name — the UNIQUE (owner_id, name) constraint
    rejects it, which surfaces here as an insert error).
    """
    clean_name = (name or "").strip()[:_NAME_MAX]
    if not clean_name:
        return None
    row = {
        "owner_id":    user_id,
        "name":        clean_name,
        "description": (description or "").strip()[:_DESC_MAX] or None,
    }
    try:
        client = get_client()
        response = client.table("projects").insert(row).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error("Failed to create project for {uid}: {err}", uid=user_id, err=e)
        return None


async def list_projects(user_id: str) -> list[dict]:
    """List a user's projects, newest first."""
    try:
        client = get_client()
        response = (
            client.table("projects")
            .select("id, name, description, created_at")
            .eq("owner_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []
    except Exception as e:
        logger.error("Failed to list projects for {uid}: {err}", uid=user_id, err=e)
        return []


async def get_project(user_id: str, project_id: str) -> Optional[dict]:
    """Fetch one project the user owns, or None. Scoped by owner_id so it
    doubles as an ownership check."""
    try:
        client = get_client()
        response = (
            client.table("projects")
            .select("id, name, description, created_at")
            .eq("id", project_id)
            .eq("owner_id", user_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error("Failed to get project {pid}: {err}", pid=project_id, err=e)
        return None


async def delete_project(user_id: str, project_id: str) -> bool:
    """Delete a project the user owns. Scoped by owner_id so one user can't
    delete another's project even though the service key bypasses RLS. Runs
    linked to it are unlinked (project_id → NULL) by the FK ON DELETE SET NULL."""
    try:
        client = get_client()
        (
            client.table("projects")
            .delete()
            .eq("id", project_id)
            .eq("owner_id", user_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.error("Failed to delete project {pid}: {err}", pid=project_id, err=e)
        return False
