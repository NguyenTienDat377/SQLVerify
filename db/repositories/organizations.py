"""
db/repositories/organizations.py

Data access layer for organizations / org_members (Migration #5/#6) — lets a
Team subscription cover every member, and lets projects/runs be shared across
an org instead of staying strictly per-user.

Access model mirrors projects/api_keys: the service-key Supabase client
bypasses RLS, so every function scopes explicitly in code. RLS policies exist
as defense-in-depth only.

Membership management (add/remove) is owner-only by design — see the
"Server-side policy" line in CLAUDE.md's Team-tier roadmap: a gate a member
could edit on themselves isn't a gate. Only the owner may add/remove seats.

Usage:
    org = await create_organization(user_id, "Acme Platform Team")
    ok, err = await add_member_by_email(user_id, org["id"], "teammate@acme.com")
    orgs = await list_organizations_for_user(user_id)
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from db.client import get_client

_NAME_MAX = 100

# Admin list_users() has no email filter (see supabase_auth's
# SyncGoTrueAdminAPI) — the only way to resolve an invite email to a user_id
# is to page through every account. Capped so a typo'd email can't turn into
# an unbounded scan; fine at this product's current scale (small org rosters,
# not a public directory lookup). Worth revisiting with a users view/RPC if
# the user base grows past a few thousand.
_EMAIL_LOOKUP_PAGE_SIZE = 200
_EMAIL_LOOKUP_MAX_PAGES = 20

# Mirrors cli/skolem_cli.py's ALL_STATUSES + DDL_CHANGED — kept as a literal
# copy rather than a shared import because this repo must never import the
# CLI (a server-side module reaching into a client package would invert the
# dependency direction cli/'s own README is built around).
_VALID_FAIL_ON_STATUSES = {"divergent", "unknown", "error", "ddl-changed"}
_DEFAULT_FAIL_ON_POLICY = "divergent"


async def get_org_owner_for_member(user_id: str) -> Optional[str]:
    """
    Return the owner_id of the org this user belongs to as a *member*
    (not as the owner themselves — an org's owner already resolves their own
    plan via their own subscription row, so this only needs to cover the
    inherited case).

    Used by `_enforce_quota` to let a Team subscription, held by the org
    owner, cover every seat: a member with no subscription of their own still
    gets `team` treatment if their org's owner has an active Team plan.
    """
    try:
        client = get_client()
        response = (
            client.table("org_members")
            .select("organizations(owner_id)")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        org = response.data[0].get("organizations")
        return org.get("owner_id") if org else None
    except Exception as e:
        logger.error("Failed to resolve org owner for member {uid}: {err}", uid=user_id, err=e)
        return None


async def create_organization(user_id: str, name: str, seat_limit: int = 5) -> Optional[dict]:
    """Create an org owned by user_id. Returns the inserted row, or None on failure."""
    clean_name = (name or "").strip()[:_NAME_MAX]
    if not clean_name:
        return None
    row = {"owner_id": user_id, "name": clean_name, "seat_limit": seat_limit}
    try:
        client = get_client()
        response = client.table("organizations").insert(row).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error("Failed to create organization for {uid}: {err}", uid=user_id, err=e)
        return None


async def list_organizations_for_user(user_id: str) -> list[dict]:
    """
    List every org this user belongs to — as owner or member — newest first.
    Each row gets a `role` field ('owner' | 'member') so the UI can show/hide
    membership-management controls without a second lookup.
    """
    try:
        client = get_client()
        owned = (
            client.table("organizations")
            .select("id, name, seat_limit, fail_on_policy, created_at")
            .eq("owner_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        owned_rows = [{**r, "role": "owner"} for r in (owned.data or [])]

        member_resp = (
            client.table("org_members")
            .select("organizations(id, name, seat_limit, fail_on_policy, created_at)")
            .eq("user_id", user_id)
            .execute()
        )
        member_rows = []
        for r in (member_resp.data or []):
            org = r.get("organizations")
            if org:
                member_rows.append({**org, "role": "member"})
        return owned_rows + member_rows
    except Exception as e:
        logger.error("Failed to list organizations for {uid}: {err}", uid=user_id, err=e)
        return []


async def list_org_ids_for_user(user_id: str) -> list[str]:
    """Every org id this user belongs to, as owner or member — used to scope
    org-shared projects and their runs."""
    orgs = await list_organizations_for_user(user_id)
    return [o["id"] for o in orgs]


async def get_organization_if_owner(user_id: str, org_id: str) -> Optional[dict]:
    """Fetch one org, only if user_id is its owner. Doubles as the
    ownership check every membership-mutating function below relies on."""
    try:
        client = get_client()
        response = (
            client.table("organizations")
            .select("id, name, owner_id, seat_limit, fail_on_policy, created_at")
            .eq("id", org_id)
            .eq("owner_id", user_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error("Failed to get organization {oid}: {err}", oid=org_id, err=e)
        return None


async def _find_user_id_by_email(email: str) -> Optional[str]:
    """Resolve an email to a Skolem user_id via the GoTrue admin API, which
    has no email filter — see the module docstring for why this pages
    through list_users() instead of a targeted lookup."""
    target = (email or "").strip().lower()
    if not target:
        return None
    try:
        client = get_client()
        page = 1
        while page <= _EMAIL_LOOKUP_MAX_PAGES:
            users = client.auth.admin.list_users(page=page, per_page=_EMAIL_LOOKUP_PAGE_SIZE)
            if not users:
                return None
            for u in users:
                if (u.email or "").lower() == target:
                    return u.id
            if len(users) < _EMAIL_LOOKUP_PAGE_SIZE:
                return None
            page += 1
        return None
    except Exception as e:
        logger.error("Failed to resolve email {email}: {err}", email=email, err=e)
        return None


async def get_email_by_id(user_id: str) -> str:
    """Best-effort email lookup for display (member lists, audit export);
    falls back to the raw id."""
    try:
        client = get_client()
        resp = client.auth.admin.get_user_by_id(user_id)
        return resp.user.email or user_id
    except Exception as e:
        logger.warning("Failed to resolve email for {uid}: {err}", uid=user_id, err=e)
        return user_id


async def list_members(owner_user_id: str, org_id: str) -> list[dict]:
    """List an org's members (owner-only — mirrors the RLS insert/delete
    policies, which are also owner-scoped). Each row carries a best-effort
    `email` for display."""
    org = await get_organization_if_owner(owner_user_id, org_id)
    if not org:
        return []
    try:
        client = get_client()
        response = (
            client.table("org_members")
            .select("id, user_id, created_at")
            .eq("org_id", org_id)
            .order("created_at")
            .execute()
        )
        rows = response.data or []
        for row in rows:
            row["email"] = await get_email_by_id(row["user_id"])
        return rows
    except Exception as e:
        logger.error("Failed to list members for org {oid}: {err}", oid=org_id, err=e)
        return []


async def add_member_by_email(owner_user_id: str, org_id: str, email: str) -> tuple[bool, Optional[str]]:
    """
    Add a member to an org by email. Returns (ok, error_message).

    Only the org's owner may add members (Server-side policy roadmap item —
    a gate a member could edit on themselves isn't a gate). Enforces
    seat_limit, counting the owner's own implicit seat.
    """
    org = await get_organization_if_owner(owner_user_id, org_id)
    if not org:
        return False, "Organization not found."

    member_user_id = await _find_user_id_by_email(email)
    if not member_user_id:
        return False, "No Skolem account found for that email."
    if member_user_id == owner_user_id:
        return False, "That's you — you're already the owner."

    try:
        client = get_client()
        current = (
            client.table("org_members")
            .select("id", count="exact")
            .eq("org_id", org_id)
            .execute()
        )
        seats_used = (current.count or 0) + 1  # +1 for the owner's own implicit seat
        if seats_used >= org["seat_limit"]:
            return False, f"Seat limit reached ({org['seat_limit']} seats)."

        client.table("org_members").insert({"org_id": org_id, "user_id": member_user_id}).execute()
        return True, None
    except Exception as e:
        logger.error("Failed to add member {email} to org {oid}: {err}", email=email, oid=org_id, err=e)
        return False, "Couldn't add that member — they may already be in the org."


async def get_org_fail_on_policy(org_id: str) -> Optional[str]:
    """
    Fetch an org's required fail-on statuses (comma-separated), or None if
    the org doesn't exist. Called server-side by /api/verify/text to attach
    `policy_fail_on` to the response — this is what makes the policy
    server-side: a caller's own --fail-on flag can only union with what this
    returns, never override or remove it (see cli/skolem_cli.py).
    """
    try:
        client = get_client()
        response = (
            client.table("organizations")
            .select("fail_on_policy")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        return response.data[0]["fail_on_policy"] if response.data else None
    except Exception as e:
        logger.error("Failed to get fail_on_policy for org {oid}: {err}", oid=org_id, err=e)
        return None


async def set_fail_on_policy(owner_user_id: str, org_id: str, fail_on: str) -> tuple[bool, Optional[str]]:
    """
    Set an org's required fail-on statuses. Owner-only (same rationale as
    add_member_by_email): a policy a member could weaken on their own project
    isn't a policy.
    """
    clean = {s.strip().lower() for s in (fail_on or "").split(",") if s.strip()}
    invalid = clean - _VALID_FAIL_ON_STATUSES
    if invalid:
        return False, f"Unknown status: {', '.join(sorted(invalid))}."
    if not clean:
        return False, "At least one status is required."

    org = await get_organization_if_owner(owner_user_id, org_id)
    if not org:
        return False, "Organization not found."

    try:
        client = get_client()
        client.table("organizations").update(
            {"fail_on_policy": ",".join(sorted(clean))}
        ).eq("id", org_id).execute()
        return True, None
    except Exception as e:
        logger.error("Failed to set fail_on_policy for org {oid}: {err}", oid=org_id, err=e)
        return False, "Couldn't update the policy."


async def remove_member(owner_user_id: str, org_id: str, member_user_id: str) -> bool:
    """Remove a member from an org. Owner-only, same rationale as add_member_by_email."""
    org = await get_organization_if_owner(owner_user_id, org_id)
    if not org:
        return False
    try:
        client = get_client()
        client.table("org_members").delete().eq("org_id", org_id).eq("user_id", member_user_id).execute()
        return True
    except Exception as e:
        logger.error("Failed to remove member {mid} from org {oid}: {err}", mid=member_user_id, oid=org_id, err=e)
        return False
