"""
api/auth.py

GitHub OAuth2 via Supabase — three endpoints:

  GET /auth/login     — redirect the browser to Supabase's GitHub OAuth page
  GET /auth/callback  — exchange the auth code for a session, set cookies
  GET /auth/logout    — clear cookies, send back to landing

Flow:
  1. User clicks "Sign in with GitHub" → GET /auth/login
  2. Supabase handles GitHub OAuth and redirects to SITE_URL/auth/callback?code=…
  3. Server exchanges the code via Supabase client, stores access_token in an
     HttpOnly cookie, then redirects to /verify
  4. Every subsequent request reads the cookie; JWTMiddleware validates it.

PKCE:
  sign_in_with_oauth returns a code_verifier when PKCE is in use.
  We store it in a short-lived HttpOnly cookie so the callback can complete
  the exchange without server-side session state.
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from supabase import create_client

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_OPTS = dict(httponly=True, samesite="lax")


def _supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_ANON_KEY"],
    )


def _is_secure(request: Request) -> bool:
    site = os.getenv("SITE_URL", str(request.base_url))
    return site.startswith("https://")


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------

@router.get("/login")
async def login(request: Request):
    """Redirect to Supabase GitHub OAuth page."""
    site_url = os.getenv("SITE_URL", str(request.base_url).rstrip("/"))
    callback_url = f"{site_url}/auth/callback"

    client = _supabase()
    oauth = client.auth.sign_in_with_oauth({
        "provider": "github",
        "options": {"redirect_to": callback_url},
    })

    logger.info("GitHub OAuth initiated → {url}", url=callback_url)
    response = RedirectResponse(url=oauth.url)

    # Store PKCE verifier if the client generated one
    if getattr(oauth, "code_verifier", None):
        response.set_cookie(
            "sb-pkce-verifier",
            oauth.code_verifier,
            max_age=300,
            secure=_is_secure(request),
            **_COOKIE_OPTS,
        )

    return response


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def callback(request: Request, code: str = None, error: str = None, error_description: str = None):
    """Exchange the OAuth code for a Supabase session and set auth cookies."""
    if error:
        logger.warning("OAuth callback error from provider: {err}", err=error_description or error)
        return RedirectResponse(url=f"/?auth_error={error_description or error}")

    if not code:
        logger.warning("OAuth callback missing code parameter")
        return RedirectResponse(url="/?auth_error=missing_code")

    try:
        client = _supabase()
        code_verifier = request.cookies.get("sb-pkce-verifier")

        exchange_args: dict = {"auth_code": code}
        if code_verifier:
            exchange_args["code_verifier"] = code_verifier

        session_resp = client.auth.exchange_code_for_session(exchange_args)
        session = session_resp.session

        if not session:
            logger.error("Code exchange returned no session")
            return RedirectResponse(url="/?auth_error=no_session")

    except Exception as e:
        logger.exception("Code exchange failed: {err}", err=e)
        return RedirectResponse(url="/?auth_error=exchange_failed")

    logger.info("User authenticated: {uid}", uid=session.user.id if session.user else "unknown")
    secure = _is_secure(request)
    response = RedirectResponse(url="/verify")

    response.set_cookie(
        "sb-access-token",
        session.access_token,
        max_age=session.expires_in or 3600,
        secure=secure,
        **_COOKIE_OPTS,
    )
    response.set_cookie(
        "sb-refresh-token",
        session.refresh_token,
        max_age=60 * 60 * 24 * 7,
        secure=secure,
        **_COOKIE_OPTS,
    )
    response.delete_cookie("sb-pkce-verifier")

    return response


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------

@router.get("/logout")
async def logout():
    """Clear auth cookies and redirect to landing."""
    response = RedirectResponse(url="/")
    response.delete_cookie("sb-access-token")
    response.delete_cookie("sb-refresh-token")
    return response
