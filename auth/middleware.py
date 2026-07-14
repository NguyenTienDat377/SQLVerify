"""
auth/middleware.py

JWT authentication middleware for SQLVerify.

Reads the Supabase access_token from the `sb-access-token` HttpOnly cookie
(set by /auth/set-session after GitHub OAuth) or from an `Authorization: Bearer`
header (for CI/API clients).

Uses PyJWKClient to fetch Supabase's public key once and cache it.
This handles both ES256 (new Supabase projects) and HS256 (older projects)
without hardcoding an algorithm or secret.

Public paths (/, /static/*, /auth/*) pass through without a token.
Protected page routes redirect to / on failure.
Protected API routes return 401 JSON on failure.
"""

import hashlib
import os
import time

import jwt
from jwt import PyJWKClient
from loguru import logger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

from db.repositories.api_keys import KEY_PREFIX, resolve_api_key

_PUBLIC_PREFIXES = ("/static", "/auth", "/api/webhooks")
_PUBLIC_EXACT = {"/", "/api/verify/health", "/pricing", "/terms", "/privacy", "/robots.txt", "/sitemap.xml", "/ping"}

# /docs, /redoc, /openapi.json are gated by ENABLE_DOCS (mirrors main.py). When
# enabled they must be auth-exempt so the browser can load Swagger UI + schema
# without a session; when disabled FastAPI never registers them, so an
# unauthenticated request falls through to a real 404 via _route_exists().
_DOCS_ENABLED = os.getenv("ENABLE_DOCS", "").lower() == "true"
_PUBLIC_PREFIXES_DOCS = ("/docs", "/redoc", "/openapi.json")

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
        logger.info("JWKS client initialised | url={url}", url=jwks_url)
    return _jwks_client


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if _DOCS_ENABLED and any(path.startswith(p) for p in _PUBLIC_PREFIXES_DOCS):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _route_exists(request: Request) -> bool:
    """True if the path+method matches a registered route. Used so an
    unauthenticated request to a *non-existent* path falls through to a real
    404 instead of being blanket-redirected home."""
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return True
    return False


def _decode_token(token: str) -> dict | None:
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
        return jwt.decode(
            token,
            signing_key.key,
            # Asymmetric only. The signing key comes from Supabase's *public* JWKS,
            # so allowing the symmetric HS256 would invite an algorithm-confusion
            # forgery (sign an HS256 token using the public key as the HMAC secret).
            # JWKS only serves asymmetric keys, so dropping HS256 locks out no one.
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=f"{supabase_url}/auth/v1",   # reject tokens from any other issuer
        )
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
    except jwt.PyJWTError as e:
        logger.debug("JWT invalid: {err}", err=e)
    except Exception as e:
        logger.warning("JWT decode unexpected error: {err}", err=e)
    return None


# ── API-key resolution with a small per-process TTL cache ────────────────────
# CI clients hammer one key; cache hash→user_id briefly to avoid a DB round-trip
# on every request. Per-process (not shared across workers, same caveat as the
# rate limiter / circuit breaker); a revoked key keeps working for up to the TTL.
_API_KEY_TTL = 60.0
_api_key_cache: dict[str, tuple[str, float]] = {}


async def _resolve_api_key_cached(raw_key: str) -> str | None:
    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    now = time.monotonic()
    hit = _api_key_cache.get(cache_key)
    if hit and hit[1] > now:
        return hit[0]
    user_id = await resolve_api_key(raw_key)
    if user_id:
        _api_key_cache[cache_key] = (user_id, now + _API_KEY_TTL)
    return user_id


class JWTMiddleware(BaseHTTPMiddleware):
    """Authenticates each non-public request via one of two paths and injects
    request.state.user_id:
      - browser session: Supabase JWT in the `sb-access-token` cookie, or a
        plain `Authorization: Bearer <jwt>`;
      - CI/API client: a per-user API key as `Authorization: Bearer sqv_...`
        or `X-API-Key: sqv_...`.
    """

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        cookie_token = request.cookies.get("sb-access-token")
        auth_header = request.headers.get("Authorization", "")
        bearer = auth_header[7:] if auth_header.startswith("Bearer ") else None
        x_api_key = request.headers.get("X-API-Key")

        user_id = None
        user_email = None
        auth_method = None

        # 1) Browser session — cookie, or a Bearer that is NOT one of our keys.
        jwt_token = cookie_token or (
            bearer if bearer and not bearer.startswith(KEY_PREFIX) else None
        )
        if jwt_token:
            payload = _decode_token(jwt_token)
            if payload:
                user_id = payload.get("sub")
                user_email = payload.get("email")
                auth_method = "session"

        # 2) Per-user API key — sqv_ Bearer or X-API-Key header.
        if not user_id:
            api_key = x_api_key or (
                bearer if bearer and bearer.startswith(KEY_PREFIX) else None
            )
            if api_key:
                resolved = await _resolve_api_key_cached(api_key)
                if resolved:
                    user_id = resolved
                    auth_method = "api_key"

        if not user_id:
            # No such route → let it fall through to a real 404 (rendered by the
            # 404 handler) rather than redirecting/401-ing on a nonexistent path.
            if not _route_exists(request):
                return await call_next(request)
            if request.url.path.startswith("/api"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized", "detail": "Missing or invalid credentials."},
                )
            return RedirectResponse(url="/?auth_error=login_required")

        request.state.user_id = user_id
        request.state.user_email = user_email
        request.state.auth_method = auth_method
        return await call_next(request)
