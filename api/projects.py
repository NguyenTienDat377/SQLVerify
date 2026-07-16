"""
api/projects.py

Per-user project management (browser/session-authenticated).

Projects group verification runs (Migration #4). Every route requires a
logged-in session; `request.state.user_id` is injected by JWTMiddleware and
scopes each operation to the current user. Responses are HTMX partials.
"""

from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates

from core.analytics import capture_project_created, capture_project_deleted
from db.repositories.projects import (
    create_project,
    delete_project,
    list_projects,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
templates = Jinja2Templates(directory="web/templates")


def _panel(request: Request, projects: list, error: str | None = None):
    """Render the projects list partial (optionally with an inline error)."""
    return templates.TemplateResponse(
        request=request,
        name="partials/projects.html",
        context={"projects": projects, "error": error},
    )


@router.get("")
async def list_user_projects(request: Request):
    user_id = request.state.user_id
    return _panel(request, await list_projects(user_id))


@router.post("")
async def create_user_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    user_id = request.state.user_id
    created = await create_project(user_id, name, description)
    error = None
    if created is None:
        # Most likely an empty/duplicate name (UNIQUE owner_id, name).
        error = "Couldn't create that project — check the name isn't blank or already used."
    else:
        capture_project_created(user_id=user_id)
    return _panel(request, await list_projects(user_id), error=error)


@router.post("/{project_id}/delete")
async def delete_user_project(request: Request, project_id: str):
    user_id = request.state.user_id
    await delete_project(user_id, project_id)
    capture_project_deleted(user_id=user_id)
    return _panel(request, await list_projects(user_id))
