"""
api/auth.py

GitHub OAuth2 via Supabase.

Supabase returns tokens in the URL hash fragment (implicit flow):
  /auth/callback#access_token=...&refresh_token=...

The hash is client-side only — the server never sees it. So /auth/callback
returns a minimal HTML page whose JavaScript reads window.location.hash,
then POSTs the tokens to /auth/set-session, which sets HttpOnly cookies
and the browser redirects to /verify.
"""

import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_OPTS = dict(httponly=True, samesite="lax")


def _is_secure(request: Request) -> bool:
    return os.getenv("SITE_URL", str(request.base_url)).startswith("https://")


# ---------------------------------------------------------------------------
# GET /auth/login  — redirect to Supabase GitHub OAuth
# ---------------------------------------------------------------------------

@router.get("/login")
async def login(request: Request):
    site_url = os.getenv("SITE_URL", str(request.base_url).rstrip("/"))
    callback_url = f"{site_url}/auth/callback"
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")

    oauth_url = (
        f"{supabase_url}/auth/v1/authorize"
        f"?provider=github"
        f"&redirect_to={callback_url}"
    )

    logger.info("GitHub OAuth initiated | callback={url}", url=callback_url)
    return RedirectResponse(url=oauth_url)


# ---------------------------------------------------------------------------
# GET /auth/callback  — extract hash tokens via JS, then POST to /auth/set-session
# ---------------------------------------------------------------------------

@router.get("/callback")
async def callback():
    """
    Supabase lands here with tokens in the URL hash.
    JS reads the hash and POSTs them to /auth/set-session.
    """
    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Signing in…</title></head>
<body>
<script>
  (function () {
    var params = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
    if (!params.access_token) {
      location.replace("/?auth_error=no_token");
      return;
    }
    fetch("/auth/set-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        access_token:  params.access_token,
        refresh_token: params.refresh_token || "",
        expires_in:    parseInt(params.expires_in || "3600", 10)
      })
    })
    .then(function(r) {
      if (r.ok) { location.replace("/verify"); }
      else       { location.replace("/?auth_error=session_failed"); }
    })
    .catch(function() { location.replace("/?auth_error=network_error"); });
  })();
</script>
<p style="font-family:sans-serif;color:#888;text-align:center;margin-top:4rem">
  Signing you in…
</p>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# POST /auth/set-session  — set HttpOnly cookies from JS-posted tokens
# ---------------------------------------------------------------------------

class SessionBody(BaseModel):
    access_token:  str
    refresh_token: Optional[str] = ""
    expires_in:    Optional[int] = 3600


@router.post("/set-session")
async def set_session(body: SessionBody, request: Request):
    logger.info("Session established via implicit flow")
    secure = _is_secure(request)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "sb-access-token",
        body.access_token,
        max_age=body.expires_in or 3600,
        secure=secure,
        **_COOKIE_OPTS,
    )
    if body.refresh_token:
        response.set_cookie(
            "sb-refresh-token",
            body.refresh_token,
            max_age=60 * 60 * 24 * 7,
            secure=secure,
            **_COOKIE_OPTS,
        )
    return response


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------

@router.get("/logout")
async def logout():
    logger.info("User logged out")
    response = RedirectResponse(url="/")
    response.delete_cookie("sb-access-token")
    response.delete_cookie("sb-refresh-token")
    return response

# ---------------------------------------------------------------------------
# API Docs
# ---------------------------------------------------------------------------
@router.get("/docs", include_in_schema=False)
async def get_swagger_docs(request: Request):
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="SQLVerify API Docs",
        oauth2_redirect_url="/auth/docs/oauth2-redirect",
    )
