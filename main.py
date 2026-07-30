"""
main.py

FastAPI application entry point.
Mounts all routers and configures CORS.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy on Render:
    Render detects uvicorn from the start command automatically.
"""

import hashlib
import os
import secrets
import time
import types
import typing
from pathlib import Path
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
import z3

from core.logger import setup_logging
from core.analytics import init_analytics, shutdown_analytics
from loguru import logger
from api.verify import (
    router as verify_router,
    limiter,
    VerifyTextRequest,
    VerifyResponse,
    MAX_BOUND,
    MAX_FILE_BYTES,
    MAX_TIMEOUT_MS,
    CICD_TIMEOUT_MS,
    DEFAULT_BOUND,
    WEB_TIMEOUT_MS,
    WEB_MAX_TIMEOUT_MS,
    FREE_TIER_MONTHLY_LIMIT,
)
from api.auth import router as auth_router
from api.keys import router as keys_router
from api.projects import router as projects_router
from api.billing import router as billing_router
from api.webhooks import router as webhooks_router
from api.stats import router as stats_router
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
        release="skolem@0.1.0",
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
        # Don't ship frame-local secrets to Sentry. The default scrubber matches
        # var names by exact denylist membership, so secret-bearing locals like
        # `raw_key` (the skm_ API key) and `access_token` (the Supabase JWT) would
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
    init_analytics()
    logger.info("Skolem starting up")
    yield
    shutdown_analytics()  # flush queued events so the last runs aren't lost
    logger.info("Skolem shutting down")

# Swagger/ReDoc are gated behind ENABLE_DOCS so the interactive console + raw
# OpenAPI schema aren't publicly reachable in production. Unset/false → FastAPI
# serves a real 404 for them (nothing to allowlist). That 404 is the actual
# access control; robots.txt is not, so neither path is listed there.
#
# They are mounted at /api-docs, NOT /docs: the generated schema is the whole
# app (auth, billing, webhooks, page routes), which is internal plumbing rather
# than a customer-facing contract, and Swagger UI renders client-side so search
# engines only ever see an empty div. /docs is reserved for the hand-written
# public documentation below — the URL developers actually guess, and the one
# that can be crawled and ranked.
_DOCS_ENABLED = os.getenv("ENABLE_DOCS", "").lower() == "true"

app = FastAPI(
    title="Skolem",
    description="Formal verification for AI-generated SQL queries.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api-docs" if _DOCS_ENABLED else None,
    redoc_url="/api-redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
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
# Google Fonts CSS/files, and same-origin everything else.
#
# script-src uses a per-request nonce (generated in security_headers, exposed to
# templates as request.state.csp_nonce) instead of 'unsafe-inline'. Only our own
# nonce-tagged <script> tags run; an injected inline script carries no valid
# nonce and is blocked — this is the finding Mozilla Observatory flags when
# 'unsafe-inline' is present in script-src. The unpkg host source coexists with
# the nonce (both are additive without 'strict-dynamic'), so HTMX still loads.
#
# style-src keeps 'unsafe-inline' deliberately: the templates use many inline
# style="" attributes (which a CSP nonce cannot cover — nonces apply only to
# <style>/<link>, not style attributes), and inline styling is not a script-
# execution vector, so Observatory does not penalise it.
#
# frame-ancestors 'none' is the modern clickjacking control (complements
# X-Frame-Options); base-uri and form-action are locked to 'self'; object-src
# 'none' blocks legacy plugins.
def _csp_header(nonce: str) -> str:
    return "; ".join((
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' https://unpkg.com",
        # Fonts are self-hosted (web/static/fonts + tokens/fonts.css), so
        # fonts.googleapis.com and fonts.gstatic.com are gone from both
        # directives — one fewer third party able to see every visitor's IP and
        # User-Agent, and one fewer origin that could serve us CSS. 'unsafe-inline'
        # stays only for the inline style="" attributes the templates still use.
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
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
# ── Static asset fingerprinting ──────────────────────────────────────────────
# The HTML is dynamic (never cached) but /static is cached hard at the edge —
# Cloudflare was observed serving styles.css with `age: 5005` under
# `max-age=14400`. A deploy therefore hands a browser NEW markup with FOUR-hour
# OLD CSS, and the two disagree: the workspace grid gained a resizer column, the
# stale sheet still declared three, so the results panel wrapped onto row 2 and
# rendered 250px wide. Nothing was wrong with either file on its own.
#
# Appending a content hash makes the URL change whenever the bytes change, which
# is a different cache key — the edge is forced to fetch from origin on the next
# deploy instead of serving a copy that no longer matches the HTML referencing
# it. Hashed once at import: these files can't change under a running process.
def _asset_version(rel_path: str) -> str:
    """Short content hash for a file under web/static, for cache busting."""
    try:
        data = (Path("web/static") / rel_path).read_bytes()
    except OSError:
        # Never let a missing asset take the app down — an unversioned URL is
        # degraded caching, not an outage.
        return "0"
    return hashlib.sha256(data).hexdigest()[:8]


_ASSET_VERSIONS = {"css/styles.css": _asset_version("css/styles.css")}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Fresh per-request nonce, stashed on request.state BEFORE the handler runs
    # so templates can stamp it onto their <script> tags (rides the same
    # request.state channel JWTMiddleware uses for user_id). The matching
    # 'nonce-…' source is added to the CSP header below.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _csp_header(nonce))
    # Stylesheets must revalidate. styles.css is fingerprinted (see
    # _asset_version) so its URL changes on every edit, but the token sheets it
    # @imports are named inside the CSS text and can never carry that query —
    # a colour change there would otherwise sit stale behind the edge cache for
    # the full max-age. They are a few KB each, so a revalidation 304 is far
    # cheaper than shipping a palette that disagrees with the markup.
    # Fonts and images are deliberately left on the long cache: they're large,
    # and they change by filename rather than in place.
    if request.url.path.startswith("/static/css/"):
        response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
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
app.include_router(stats_router)


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
    logger.info("Skolem starting up")


@app.on_event("shutdown")
async def shutdown():
    logger.info("Skolem shutting down")


_SITE_URL = os.getenv("SITE_URL", "https://skolem.dev").rstrip("/")
templates.env.globals["site_url"] = _SITE_URL
# {{ static_v('css/styles.css') }} → an 8-char content hash. See _asset_version.
templates.env.globals["static_v"] = lambda p: _ASSET_VERSIONS.get(p) or _asset_version(p)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    # Crawlers fetch robots.txt from the domain root, so it can't live under
    # /static. Keep the private/auth/API surface out of the index; everything
    # else (/, /pricing, /integrations, /docs, /terms, /privacy) is crawlable.
    #
    # Deliberately NOT disallowed: /docs and /openapi.json. Every Disallow is a
    # PREFIX match, so a "Disallow: /docs" line would silently block the whole
    # /docs/* documentation tree — the organic surface we most want crawled —
    # the moment it grows a second page. It also bought nothing: robots.txt is
    # a request to well-behaved crawlers, never access control. What keeps the
    # OpenAPI schema private is ENABLE_DOCS being unset in prod (a real 404).
    body = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /verify\n"
        "Disallow: /keys\n"
        "Disallow: /projects\n"
        "Disallow: /billing\n"
        "\n"
        f"Sitemap: {_SITE_URL}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


# Sitemap entries: (path, lastmod). Deliberately no <priority> or <changefreq> —
# Google ignores both outright. They were only ever hints, and since every site
# on the web set them to 1.0/daily they stopped carrying any signal at all.
#
# <lastmod> IS used, but only for as long as it stays trustworthy: a sitemap
# that claims everything changed today teaches Google the field is noise, after
# which it's discounted for the whole site. So these dates are maintained BY
# HAND and must only move when the page's content genuinely changes. Being
# stale is the safe failure (Google just recrawls on its own schedule); being
# always-today is the harmful one.
#
# Do NOT "improve" this by deriving the date from file mtime or `git log`.
# .dockerignore excludes .git/, so there is no history in the container, and
# Render builds from a fresh clone — every file's mtime is the deploy time.
# Either route would stamp all six pages with today's date on every deploy,
# which is exactly the self-defeating pattern described above.
_SITEMAP_PAGES = [
    # Bumped 2026-07-30: landing, pricing, docs and integrations were all
    # rewritten in the design-system port (new content, not just restyling).
    # terms/privacy are untouched and keep their dates — bumping those too is
    # exactly the noise that teaches Google to ignore lastmod site-wide.
    ("/",             "2026-07-30"),
    ("/docs",         "2026-07-30"),
    ("/integrations", "2026-07-30"),
    ("/pricing",      "2026-07-30"),
    ("/terms",        "2026-06-20"),
    ("/privacy",      "2026-06-20"),
]


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    urls = "".join(
        f"<url><loc>{_SITE_URL}{path}</loc><lastmod>{lastmod}</lastmod></url>"
        for path, lastmod in _SITEMAP_PAGES
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=body, media_type="application/xml")


# The landing page states the solver version and the engine's real limits
# instead of hardcoding them into the markup — the design mockup carried
# invented figures (z3 4.13.0, "bound 1–8"), and a marketing claim that drifts
# from the engine is worse than no claim on a product selling correctness.
# Resolved once at import; z3 is already loaded via api.verify → core.
_Z3_VERSION = z3.get_version_string()


@app.get("/")
async def root(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "user_email": user_email,
            "z3_version": _Z3_VERSION,
            "default_bound": DEFAULT_BOUND,
            "max_bound": MAX_BOUND,
            "free_tier_limit": FREE_TIER_MONTHLY_LIMIT,
        },
    )


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
        context={
            "user_email": user_email,
            "projects": projects,
            # Advanced settings are rendered from the server's own limits rather
            # than hand-written options, so a change to MAX_BOUND or the web
            # timeout ceiling can never leave the form offering a value the
            # endpoint would 422 on.
            "default_bound": DEFAULT_BOUND,
            "max_bound": MAX_BOUND,
            "default_timeout_ms": WEB_TIMEOUT_MS,
            "max_timeout_ms": WEB_MAX_TIMEOUT_MS,
        },
    )


@app.get("/pricing")
async def pricing_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    # Same rule as the landing page: a pricing page states what the code
    # enforces, so the numbers come from the quota gate and the encoder rather
    # than from the markup. The free limit in particular is env-tunable
    # (FREE_TIER_MONTHLY_LIMIT) — a hardcoded "100" here would start lying the
    # first time that env var is changed on Render, on the one page where a
    # wrong number is a billing dispute.
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context={
            "user_email": user_email,
            "free_tier_limit": FREE_TIER_MONTHLY_LIMIT,
            "default_bound": DEFAULT_BOUND,
            "max_bound": MAX_BOUND,
            "web_timeout_s": WEB_TIMEOUT_MS // 1000,
            "web_max_timeout_s": WEB_MAX_TIMEOUT_MS // 1000,
            "ci_timeout_s": CICD_TIMEOUT_MS // 1000,
            "ci_max_timeout_s": MAX_TIMEOUT_MS // 1000,
        },
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


# --------------------------------------------------------------------------
# Public documentation (/docs)
#
# The field tables on this page are introspected from the very Pydantic models
# the endpoint actually uses, so a renamed/retyped/added field shows up in the
# docs on the next request instead of rotting into a lie. Only the prose
# descriptions are hand-written; a field with no entry in the maps below renders
# an empty cell, which is the visible nudge to write one.
# --------------------------------------------------------------------------

def _type_label(annotation) -> str:
    """Render a Pydantic annotation as a JSON-ish type name (`str` → string)."""
    origin = typing.get_origin(annotation)
    is_union = origin is typing.Union or (
        hasattr(types, "UnionType") and origin is getattr(types, "UnionType")
    )
    if is_union:
        args = typing.get_args(annotation)
        inner = " | ".join(_type_label(a) for a in args if a is not type(None))
        return f"{inner} | null" if type(None) in args else inner
    if origin is not None:  # list[str], dict[str, int], …
        annotation = origin
    return {
        str: "string", int: "integer", float: "number",
        bool: "boolean", dict: "object", list: "array",
    }.get(annotation, getattr(annotation, "__name__", str(annotation)))


def _describe_model(model, descriptions: dict) -> list:
    rows = []
    for name, field in model.model_fields.items():
        required = field.is_required()
        rows.append({
            "name": name,
            "type": _type_label(field.annotation),
            "required": required,
            "default": None if required else field.default,
            "description": descriptions.get(name, ""),
        })
    return rows


_REQUEST_FIELD_DOCS = {
    "ddl_sql": "Flyway-style CREATE TABLE DDL defining the schema both queries run "
               "against. ALTER TABLE statements are folded in statement order.",
    "sql_v1": "The original / trusted SELECT query.",
    "sql_v2": "The rewritten query to prove equivalent to sql_v1.",
    "dialect": "Parser dialect hint. Both queries are read as the same dialect — "
               "cross-dialect comparison is out of scope.",
    "bound": f"Maximum rows per table Z3 explores. Higher catches rarer bugs and "
             f"costs solve time. Values above {MAX_BOUND} are rejected.",
    "timeout_ms": f"Solver budget in milliseconds. Clamped to {MAX_TIMEOUT_MS:,}. "
                  f"On timeout the status is unknown — never a wrong verdict.",
    "project_id": "Optional project UUID to tag the run with. Silently dropped if "
                  "the project isn't yours.",
}

_RESPONSE_FIELD_DOCS = {
    "status": "equivalent, divergent, unknown, or error. See the table below.",
    "divergence_reason": "Short summary of how the two queries differ. Present when "
                         "status is divergent.",
    "counterexample_db": "A concrete database — table name to rows — on which the two "
                         "queries disagree. This is the proof, and it is replayable.",
    "query_v1_output": "Rows sql_v1 returns when run on counterexample_db.",
    "query_v2_output": "Rows sql_v2 returns when run on counterexample_db.",
    "error_message": "Why the input was rejected. Present when status is error.",
    "explanation": "Prose explanation of the divergence, written by the configured "
                   "LLM. Populated automatically for divergent results.",
}


@app.get("/docs")
async def docs_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="docs.html",
        context={
            "user_email": user_email,
            "request_fields": _describe_model(VerifyTextRequest, _REQUEST_FIELD_DOCS),
            "response_fields": _describe_model(VerifyResponse, _RESPONSE_FIELD_DOCS),
            "max_bound": MAX_BOUND,
            "max_timeout_ms": MAX_TIMEOUT_MS,
            "default_timeout_ms": CICD_TIMEOUT_MS,
            "max_file_kb": MAX_FILE_BYTES // 1024,
            # The semantics reference quotes the default bound and the solver
            # version in prose; both come from the engine so the page can't drift.
            "default_bound": DEFAULT_BOUND,
            "z3_version": _Z3_VERSION,
        },
    )


@app.get("/integrations")
async def integrations_page(request: Request):
    user_email = getattr(request.state, "user_email", None)
    return templates.TemplateResponse(
        request=request,
        name="integrations.html",
        context={"user_email": user_email},
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
