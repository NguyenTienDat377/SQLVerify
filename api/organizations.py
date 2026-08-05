"""
api/organizations.py

Per-user organization management (browser/session-authenticated).

Organizations back the Team tier: one owner, up to `seat_limit` members, and
(via db/repositories/projects.py + verification_runs.py) shared projects and
run history. Membership add/remove is owner-only by design — see the
"Server-side policy" line in CLAUDE.md's Team-tier roadmap. Every route
requires a logged-in session; `request.state.user_id` is injected by
JWTMiddleware. Responses are HTMX partials.
"""

import csv
import io

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import StreamingResponse

from fastapi.templating import Jinja2Templates

from core.analytics import (
    capture_org_audit_exported,
    capture_org_created,
    capture_org_member_added,
    capture_org_member_removed,
)
from db.repositories.organizations import (
    add_member_by_email,
    create_organization,
    get_email_by_id,
    get_organization_if_owner,
    list_members,
    list_organizations_for_user,
    remove_member,
    set_fail_on_policy,
)
from db.repositories.verification_runs import get_org_audit_rows

router = APIRouter(prefix="/api/organizations", tags=["organizations"])
templates = Jinja2Templates(directory="web/templates")

# Spreadsheet apps (Excel, Sheets) treat a cell starting with one of these as
# a formula. divergence_reason/project name ultimately trace back to
# user-supplied SQL identifiers, so a crafted table/column name could smuggle
# a formula into an exported CSV opened by a spreadsheet — prefixing with a
# quote defuses it without changing the visible text.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + s
    return s


def _panel(request: Request, orgs: list, error: str | None = None):
    """Render the organizations list partial (optionally with an inline error)."""
    return templates.TemplateResponse(
        request=request,
        name="partials/organizations.html",
        context={"orgs": orgs, "error": error},
    )


@router.get("")
async def list_user_organizations(request: Request):
    user_id = request.state.user_id
    return _panel(request, await list_organizations_for_user(user_id))


@router.post("")
async def create_user_organization(request: Request, name: str = Form(...)):
    user_id = request.state.user_id
    created = await create_organization(user_id, name)
    error = None
    if created is None:
        error = "Couldn't create that organization — check the name isn't blank."
    else:
        capture_org_created(user_id=user_id)
    return _panel(request, await list_organizations_for_user(user_id), error=error)


@router.get("/{org_id}/members")
async def get_org_members(request: Request, org_id: str):
    user_id = request.state.user_id
    members = await list_members(user_id, org_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/org_members.html",
        context={"org_id": org_id, "members": members, "error": None},
    )


@router.post("/{org_id}/members")
async def add_org_member(request: Request, org_id: str, email: str = Form(...)):
    user_id = request.state.user_id
    ok, error = await add_member_by_email(user_id, org_id, email)
    if ok:
        capture_org_member_added(user_id=user_id)
    members = await list_members(user_id, org_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/org_members.html",
        context={"org_id": org_id, "members": members, "error": None if ok else error},
    )


@router.post("/{org_id}/members/{member_user_id}/remove")
async def remove_org_member(request: Request, org_id: str, member_user_id: str):
    user_id = request.state.user_id
    await remove_member(user_id, org_id, member_user_id)
    capture_org_member_removed(user_id=user_id)
    members = await list_members(user_id, org_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/org_members.html",
        context={"org_id": org_id, "members": members, "error": None},
    )


@router.get("/{org_id}/audit.csv")
async def export_org_audit(request: Request, org_id: str):
    """
    Owner-only CSV export of every run on a project scoped to this org — who
    proved what, when, what verdict (CLAUDE.md Team-tier decision table,
    item #6). Pure margin: every row already exists in verification_runs,
    this just joins and formats it.
    """
    user_id = request.state.user_id
    org = await get_organization_if_owner(user_id, org_id)
    if not org:
        raise HTTPException(status_code=404)

    rows = await get_org_audit_rows(org_id)

    # Resolve each distinct user_id to an email once rather than per row —
    # an org has at most a handful of members, but could have many runs.
    emails: dict = {}
    for row in rows:
        uid = row.get("user_id")
        if uid and uid not in emails:
            emails[uid] = await get_email_by_id(uid)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["run_id", "created_at", "user_email", "project",
                      "status", "divergence_reason", "duration_ms"])
    for row in rows:
        project_name = (row.get("projects") or {}).get("name", "")
        writer.writerow([
            _csv_safe(row.get("id")),
            _csv_safe(row.get("created_at")),
            _csv_safe(emails.get(row.get("user_id"), row.get("user_id"))),
            _csv_safe(project_name),
            _csv_safe(row.get("status")),
            _csv_safe(row.get("divergence_reason")),
            row.get("duration_ms") if row.get("duration_ms") is not None else "",
        ])
    buf.seek(0)

    capture_org_audit_exported(user_id=user_id)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="skolem-audit-{org_id}.csv"'},
    )


@router.post("/{org_id}/policy")
async def update_org_policy(request: Request, org_id: str, fail_on: str = Form(...)):
    """Owner-only: set the statuses that must fail a check for every project
    scoped to this org (server-side, so a member's own --fail-on can only
    add to it — see cli/skolem_cli.py's _effective_fail_on)."""
    user_id = request.state.user_id
    ok, error = await set_fail_on_policy(user_id, org_id, fail_on)
    orgs = await list_organizations_for_user(user_id)
    return _panel(request, orgs, error=None if ok else error)
