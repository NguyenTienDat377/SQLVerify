"""
auth/middleware.py

JWT authentication middleware for SQLVerify.

Reads the Supabase access_token from the `sb-access-token` HttpOnly cookie
(set by /auth/callback after GitHub OAuth) or from an `Authorization: Bearer`
header (for CI/API clients).

Verifies the JWT locally using SUPABASE_JWT_SECRET — no network round-trip per
request. On success, injects request.state.user_id and request.state.user_email.

Public paths (/, /static/*, /auth/*) pass through without a token.
Protected page routes redirect to / on failure.
Protected API routes return 401 JSON on failure.
"""

import os

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Paths that never require authentication
_PUBLIC_PREFIXES = ("/static", "/auth")
_PUBLIC_EXACT = {"/", "/docs", "/openapi.json", "/api/verify/health"}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _decode_token(token: str) -> dict | None:
    secret = os.getenv("SUPABASE_JWT_SECRET", "")
    if not secret:
        return None
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
    except jwt.PyJWTError:
        return None


class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        # Accept token from cookie (browser) or Authorization header (API clients)
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
            # Page route — send back to landing so the user can log in
            return RedirectResponse(url="/")

        request.state.user_id = payload.get("sub")
        request.state.user_email = payload.get("email")
        return await call_next(request)
