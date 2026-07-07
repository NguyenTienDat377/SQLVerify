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

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from loguru import logger
from pydantic import BaseModel

# Share the single app limiter (registered on app.state.limiter in main.py) rather
# than spinning up a second instance — one bucket, one source of truth.
from api.verify import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_OPTS = dict(httponly=True, samesite="lax")

def _is_secure(request: Request) -> bool:
    return os.getenv("SITE_URL", str(request.base_url)).startswith("https://")

LOGIN_RATE_LIMIT = "5/minute"
# magic-link triggers a real OTP email send — throttle it harder than the
# credential-less OAuth redirect to blunt inbox-bombing / email-quota abuse.
MAGIC_LINK_RATE_LIMIT = "5/minute"

# ---------------------------------------------------------------------------
# GET /auth/login  — redirect to Supabase GitHub OAuth
# ---------------------------------------------------------------------------

@router.get("/login")
@limiter.limit(LOGIN_RATE_LIMIT)
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
# POST /auth/magic-link  — email a passwordless sign-in link
# ---------------------------------------------------------------------------

@router.post("/magic-link")
@limiter.limit(MAGIC_LINK_RATE_LIMIT)
async def magic_link(request: Request, email: str = Form(...)):
    """
    Trigger a Supabase magic-link (OTP) email. The emailed link lands on the
    existing /auth/callback with tokens in the URL hash — the same implicit
    flow as GitHub OAuth — so no new token handling is needed here.
    """
    site_url = os.getenv("SITE_URL", str(request.base_url).rstrip("/"))
    callback_url = f"{site_url}/auth/callback"
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    anon_key = os.environ["SUPABASE_ANON_KEY"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{supabase_url}/auth/v1/otp",
                params={"redirect_to": callback_url},
                headers={"apikey": anon_key, "Content-Type": "application/json"},
                json={"email": email, "create_user": True},
            )
        if resp.status_code >= 400:
            logger.warning(
                "Magic-link request rejected {code}: {body}",
                code=resp.status_code, body=resp.text,
            )
    except httpx.HTTPError as e:
        logger.warning("Magic-link request failed: {err}", err=e)

    # Respond identically regardless of outcome — don't leak whether an address
    # is registered, and keep the UX simple ("go check your email").
    if "hx-request" in request.headers:
        # No inline <script> here: this fragment is swapped in by HTMX, and an
        # injected inline script carries no valid CSP nonce (script-src has no
        # 'unsafe-inline'), so it would be blocked. The toast is auto-dismissed
        # by a page-level htmx:afterSwap handler in landing.html instead.
        return HTMLResponse(
            """
            <div class="toast-popup" id="magic-link-toast">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--success-color); flex-shrink: 0;"><polyline points="20 6 9 17 4 12"></polyline></svg>
              <span>Check your inbox &mdash; a sign-in link is on its way.</span>
            </div>
            """
        )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# GET /auth/callback  — extract hash tokens via JS, then POST to /auth/set-session
# ---------------------------------------------------------------------------

@router.get("/callback")
async def callback(request: Request):
    """
    Supabase lands here with tokens in the URL hash.
    JS reads the hash and POSTs them to /auth/set-session.

    This is raw HTML (not a Jinja template), so the CSP nonce is injected
    manually: the app's script-src has no 'unsafe-inline', so without a
    matching nonce the browser would block this script and login would hang.
    """
    nonce = getattr(request.state, "csp_nonce", "")
    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Signing in…</title></head>
<body>
<script nonce="__CSP_NONCE__">
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
    return HTMLResponse(content=html.replace("__CSP_NONCE__", nonce))


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
# POST /auth/logout  — POST (not GET) so it can't be triggered by a cross-site
# <img>/link/prefetch (CSRF-logout). 303 redirects the browser to GET "/".
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout():
    logger.info("User logged out")
    response = RedirectResponse(url="/", status_code=303)
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
