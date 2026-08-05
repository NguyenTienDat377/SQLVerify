"""
db/repositories/projects.py

Data access layer for the projects table — per-user projects that group
verification runs (Migration #4), optionally shared across an org
(Migration #6, org_id).

Access model mirrors api_keys / verification_runs: the service-key Supabase
client bypasses RLS, so every function scopes by owner_id (or org membership)
in code. RLS policies exist as defense-in-depth only.

org_id is trusted here — the caller (api/projects.py) is responsible for
validating that the requesting user actually belongs to that org before
passing it in, the same way api/verify.py validates project_id ownership
before calling save_run().

Usage:
    proj = await create_project(user_id, "Payments API", "Billing service queries")
    rows = await list_projects(user_id)  # own + any org-shared projects
    await delete_project(user_id, project_id)
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from db.client import get_client
from db.repositories.organizations import list_org_ids_for_user

_NAME_MAX = 100
_DESC_MAX = 500
_SELECT_FIELDS = "id, name, description, org_id, created_at"


async def create_project(
    user_id: str, name: str, description: str | None = None, org_id: str | None = None
) -> Optional[dict]:
    """
    Create a project owned by user_id, optionally scoped to an org so every
    member sees it (and its runs). Returns the inserted row, or None on
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
        "org_id":      org_id,
    }
    try:
        client = get_client()
        response = client.table("projects").insert(row).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error("Failed to create project for {uid}: {err}", uid=user_id, err=e)
        return None


async def list_projects(user_id: str) -> list[dict]:
    """List a user's own projects plus any project shared via an org they
    belong to, newest first."""
    try:
        client = get_client()
        org_ids = await list_org_ids_for_user(user_id)
        query = client.table("projects").select(_SELECT_FIELDS)
        if org_ids:
            org_list = ",".join(org_ids)
            query = query.or_(f"owner_id.eq.{user_id},org_id.in.({org_list})")
        else:
            query = query.eq("owner_id", user_id)
        response = query.order("created_at", desc=True).execute()
        return response.data or []
    except Exception as e:
        logger.error("Failed to list projects for {uid}: {err}", uid=user_id, err=e)
        return []


async def get_project(user_id: str, project_id: str) -> Optional[dict]:
    """Fetch one project the user can access — as owner, or as a member of
    the org it's scoped to. Doubles as an ownership/access check (used by
    api/verify.py's _resolve_project_id before tagging a run)."""
    try:
        client = get_client()
        response = (
            client.table("projects")
            .select(_SELECT_FIELDS)
            .eq("id", project_id)
            .eq("owner_id", user_id)
            .limit(1)
            .execute()
        )
        if response.data:
            return response.data[0]

        # Not the owner — check whether it's shared via an org this user belongs to.
        response = (
            client.table("projects")
            .select(_SELECT_FIELDS)
            .eq("id", project_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        project = response.data[0]
        if not project.get("org_id"):
            return None
        org_ids = await list_org_ids_for_user(user_id)
        return project if project["org_id"] in org_ids else None
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
