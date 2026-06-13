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

import os

import jwt
from jwt import PyJWKClient
from loguru import logger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

_PUBLIC_PREFIXES = ("/static", "/auth", "/api/webhooks")
_PUBLIC_EXACT = {"/", "/docs", "/openapi.json", "/api/verify/health", "/pricing"}

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
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _decode_token(token: str) -> dict | None:
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256", "HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
    except jwt.PyJWTError as e:
        logger.debug("JWT invalid: {err}", err=e)
    except Exception as e:
        logger.warning("JWT decode unexpected error: {err}", err=e)
    return None


class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        token = request.cookies.get("sb-access-token")
        if not token:
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                token = header[7:]

        payload = _decode_token(token) if token else None

        if not payload:
            if request.url.path.startswith("/api"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized", "detail": "Missing or invalid token."},
                )
            return RedirectResponse(url="/")

        request.state.user_id = payload.get("sub")
        request.state.user_email = payload.get("email")
        return await call_next(request)
