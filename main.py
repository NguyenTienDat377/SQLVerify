"""
main.py

FastAPI application entry point.
Mounts all routers and configures CORS.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy on Render:
    Render detects uvicorn from the start command automatically.
"""

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.logger import setup_logging
from loguru import logger
from api.verify import router as verify_router, limiter
from api.auth import router as auth_router
from api.keys import router as keys_router
from api.projects import router as projects_router
from api.billing import router as billing_router
from api.webhooks import router as webhooks_router
from auth.middleware import JWTMiddleware
from db.repositories.api_keys import list_api_keys
from db.repositories.projects import list_projects

setup_logging()

# Sentry — error tracking only (no metrics/dashboards; see CLAUDE.md health
# monitoring decision). A no-op when SENTRY_DSN is unset, so local dev and tests
# run untouched. The FastAPI integration auto-captures unhandled exceptions that
# propagate through ServerErrorMiddleware (our custom 500 handler re-raises), so
# the latent crash-class bugs surface with stack traces. traces_sample_rate is
# low to keep performance-monitoring cost down — bump it if you want APM later.
_SENTRY_DSN = os.getenv("SENTRY_DSN")
if _SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release="sqlverify@0.1.0",
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
        # Don't ship frame-local secrets to Sentry. The default scrubber matches
        # var names by exact denylist membership, so secret-bearing locals like
        # `raw_key` (the sqv_ API key) and `access_token` (the Supabase JWT) would
        # otherwise leak verbatim on any exception in the auth path.
        include_local_variables=False,
    )
    logger.info("Sentry initialized (env={})", os.getenv("SENTRY_ENVIRONMENT", "production"))


# Vars the app cannot run correctly without. Validated at boot so a misconfigured
# deploy fails fast and loudly here, instead of throwing a 500 on the first user
# request after Render has already reported the service "healthy".
_REQUIRED_ENV = (
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_KEY",
)


def _validate_required_env() -> None:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_required_env()
    logger.info("SQLVerify starting up")
    yield
    logger.info("SQLVerify shutting down")

app = FastAPI(
    title="SQLVerify",
    description="Formal verification for AI-generated SQL queries.",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting (slowapi): the limiter is defined with the verify routes; it must
# be attached to the serving app and given the 429 handler, or @limiter.limit
# decorators raise at request time. In-memory storage is fine for a single
# always-on container (per CLAUDE.md); a multi-instance deploy needs a shared
# store (e.g. storage_uri="redis://...").
app.state.limiter = limiter
# slowapi's handler signature is narrower than FastAPI's ExceptionHandler type;
# this is the documented wiring and correct at runtime.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# CORS — pinned to known origins (no wildcard). SITE_URL is the prod frontend;
# localhost covers dev. Cookies are HttpOnly and we don't enable credentialed
# CORS, but an explicit allowlist avoids exposing JSON responses to any origin.
_cors_origins = [
    o for o in (os.getenv("SITE_URL"), "http://localhost:8000", "http://127.0.0.1:8000")
    if o
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# JWT auth middleware
app.add_middleware(JWTMiddleware)


# Content-Security-Policy. Scoped to exactly what the app loads: HTMX from unpkg,
# Google Fonts CSS/files, and same-origin everything else. 'unsafe-inline' is
# required because the templates use inline <script>/<style> and onclick handlers
# (a strict nonce-based policy would need those refactored). frame-ancestors
# 'none' is the modern clickjacking control (complements X-Frame-Options); base-uri
# and form-action are locked to 'self'; object-src 'none' blocks legacy plugins.
_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
))


# Security response headers on every response. HSTS is only emitted over HTTPS
# (request.url.scheme reflects X-Forwarded-Proto thanks to uvicorn
# --forwarded-allow-ips), so local http dev is untouched and we never pin a
# browser to HTTPS for a host that can't serve it.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

@app.get("/ping", include_in_schema=False)
async def ping():
    return Response(content="ok", media_type="text/plain")

# Routers
app.include_router(verify_router)
app.include_router(auth_router)
app.include_router(keys_router)
app.include_router(projects_router)
app.include_router(billing_router)
app.include_router(webhooks_router)


# Mount static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

# Templates
templates = Jinja2Templates(directory="web/templates")


# Custom error pages. 404 (and other HTTP errors) flow through ExceptionMiddleware;
# unhandled exceptions become 500 via ServerErrorMiddleware. API paths keep JSON;
# browser pages get the rendered templates.
# Custom error pages. The 404 handler (keyed by status) is invoked by
# ExceptionMiddleware when the router finds no match; the 500 handler is wired
# into ServerErrorMiddleware by Starlette, so it also catches *unhandled*
# exceptions (a raw `raise`), not just an explicit HTTPException(500). API paths
# keep JSON; browser pages get the rendered templates.
@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    if request.url.path.startswith("/api"):
        return JSONResponse(status_code=404, content={"error": "Not Found"})
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request, name="404.html", context={"user_email": user_email}, status_code=404
    )


@app.exception_handler(500)
async def custom_500_handler(request: Request, exc):
    logger.exception("Unhandled error on {path}", path=request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request, name="500.html", context={"user_email": user_email}, status_code=500
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "{method} {path} → {status} ({duration}ms)",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration=duration_ms,
    )
    return response


@app.on_event("startup")
async def startup():
    logger.info("SQLVerify starting up")


@app.on_event("shutdown")
async def shutdown():
    logger.info("SQLVerify shutting down")


_SITE_URL = os.getenv("SITE_URL", "https://sqlverify.com").rstrip("/")
templates.env.globals["site_url"] = _SITE_URL


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    # Crawlers fetch robots.txt from the domain root, so it can't live under
    # /static. Keep the private/auth/API surface out of the index; everything
    # else (/, /pricing, /terms, /privacy) is crawlable by default.
    body = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /verify\n"
        "Disallow: /keys\n"
        "Disallow: /projects\n"
        "Disallow: /billing\n"
        "Disallow: /docs\n"
        "Disallow: /openapi.json\n"
        "\n"
        f"Sitemap: {_SITE_URL}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    pages = [
        ("/", "weekly", "1.0"),
        ("/pricing", "monthly", "0.8"),
        ("/terms", "yearly", "0.3"),
        ("/privacy", "yearly", "0.3"),
    ]
    urls = "".join(
        f"<url><loc>{_SITE_URL}{path}</loc>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in pages
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/verify")
async def verify_page(request: Request):
    user_id = getattr(request.state, "user_id", None)
    user_email = getattr(request.state, "user_email", None)
    # Populate the project selector; a new run is tagged with the chosen project
    # and the History tab filters by it.
    projects = await list_projects(user_id) if user_id else []
    return templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={"user_email": user_email, "projects": projects},
    )


@app.get("/pricing")
async def pricing_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context={"user_email": user_email},
    )


@app.get("/terms")
async def terms_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="terms.html",
        context={"user_email": user_email},
    )


@app.get("/privacy")
async def privacy_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={"user_email": user_email},
    )


@app.get("/keys")
async def keys_page(request: Request):
    # Protected by JWTMiddleware (not a public path) — user_id is always set here.
    user_id = getattr(request.state, "user_id", None)
    user_email = getattr(request.state, "user_email", None)
    keys = await list_api_keys(user_id) if user_id else []
    return templates.TemplateResponse(
        request=request,
        name="keys.html",
        context={"user_email": user_email, "keys": keys},
    )


@app.get("/projects")
async def projects_page(request: Request):
    # Protected by JWTMiddleware (not a public path) — user_id is always set here.
    user_id = getattr(request.state, "user_id", None)
    user_email = getattr(request.state, "user_email", None)
    projects = await list_projects(user_id) if user_id else []
    return templates.TemplateResponse(
        request=request,
        name="projects.html",
        context={"user_email": user_email, "projects": projects},
    )
