"""
api/keys.py

Per-user API-key management (browser/session-authenticated).

API keys are the second auth path for CI/CD clients calling
`POST /api/verify/text` — see auth/middleware.py. Every route here requires a
logged-in session; `request.state.user_id` is injected by JWTMiddleware and
scopes each operation to the current user. Responses are HTMX partials.
"""

from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates

from db.repositories.api_keys import create_api_key, list_api_keys, revoke_api_key

router = APIRouter(prefix="/api/keys", tags=["api-keys"])
templates = Jinja2Templates(directory="web/templates")


def _panel(request: Request, keys: list, new_key: str | None = None):
    """Render the keys panel partial (optionally with a show-once raw key)."""
    return templates.TemplateResponse(
        request=request,
        name="partials/api_keys.html",
        context={"keys": keys, "new_key": new_key},
    )


@router.get("")
async def list_keys(request: Request):
    user_id = request.state.user_id
    return _panel(request, await list_api_keys(user_id))


@router.post("")
async def create_key(request: Request, name: str = Form("API key")):
    user_id = request.state.user_id
    raw_key, _ = await create_api_key(user_id, name)
    # raw_key is shown exactly once, in the returned partial.
    return _panel(request, await list_api_keys(user_id), new_key=raw_key)


@router.post("/{key_id}/revoke")
async def revoke_key(request: Request, key_id: str):
    user_id = request.state.user_id
    await revoke_api_key(user_id, key_id)
    return _panel(request, await list_api_keys(user_id))
