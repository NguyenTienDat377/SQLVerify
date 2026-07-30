"""
api/verify.py

POST /api/verify  — Direction 1: migration equivalence check
POST /api/verify/constraint — Direction 2: single query constraint check (stub for now)

Accepts multipart file uploads:
  - schema_file:  Flyway DDL (.sql)
  - query_v1:     Original trusted query (.sql or raw text)
  - query_v2:     AI-generated query (.sql or raw text)
  - dialect:      SQL dialect string (default: 'generic')
  - bound:        Z3 bound (default: 3, max: 6)

Returns JSON:
  {
    "status": "divergent" | "equivalent" | "unknown" | "error",
    "divergence_reason": "...",
    "counterexample_db": { "accounts": [...], "transactions": [...] },
    "query_v1_output": [...],
    "query_v2_output": [...],
    "error_message": null
  }
"""

import os
import time
import markdown
import nh3
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from loguru import logger
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address

from core.analytics import (
    capture_verification_run,
    capture_quota_exceeded,
    capture_explanation_requested,
)
from core.equivalence import check_equivalence
from core.models import VerificationResult
# The engine's own default, not a retyped literal: the UI's Advanced-settings
# panel and the docs page both render it, so a change in the encoder must not
# leave three copies disagreeing about what "default" means.
from core.sql_encoder import DEFAULT_BOUND

from explainer.explain import explain_result

from db.repositories.verification_runs import (
    save_run, get_recent_runs, update_explanation, count_runs_this_month,
)
from db.repositories.subscriptions import get_active_subscription_by_user
from db.repositories.projects import get_project


templates = Jinja2Templates(directory="web/templates")
templates.env.filters["markdown"] = lambda text: nh3.clean(markdown.markdown(text)) if text else ""

router = APIRouter(prefix="/api", tags=["verify"])

# Per-IP rate limiter. It lives here with the routes, but is attached to the
# real application (app.state.limiter) and given its exception handler in
# main.py — that is the app that actually serves requests.
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class VerifyResponse(BaseModel):
    status: str
    divergence_reason: Optional[str] = None
    counterexample_db: Optional[dict] = None
    query_v1_output: Optional[list] = None
    query_v2_output: Optional[list] = None
    error_message: Optional[str] = None
    # Human-readable LLM explanation, already generated for divergent results
    # (see verify_equivalence_text). Null when not applicable/unavailable.
    explanation: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 512 * 1024  # 512 KB per file — SQL files are never bigger
MAX_BOUND = 6

# Per-surface Z3 timeouts. The web UI favours a snappy response (a human is
# waiting), so it caps lower; CI/CD favours a definitive verdict over latency,
# so it allows the solver longer before degrading to `unknown`. Deep INNER-join
# chains (bound^n) are the main driver of long solves — see CLAUDE.md.
WEB_TIMEOUT_MS = 15_000       # default for the interactive surface (POST /api/verify)
WEB_MAX_TIMEOUT_MS = 60_000   # web ceiling — keep the UI responsive
CICD_TIMEOUT_MS = 60_000      # default for the pipeline surface (POST /api/verify/text)
MAX_TIMEOUT_MS = 120_000      # hard ceiling for caller-supplied CI/CD timeouts

# Per-IP rate limit on the two solve endpoints. Verification is expensive (a Z3
# solve can run up to MAX_TIMEOUT_MS), so an unthrottled endpoint lets one
# client pin a worker. Shared by both surfaces; tune to taste.
VERIFY_RATE_LIMIT = "30/minute"

# Free-tier quota: verification runs per calendar month before an upgrade is
# required. Any active paid subscription lifts the cap entirely.
FREE_TIER_MONTHLY_LIMIT = int(os.getenv("FREE_TIER_MONTHLY_LIMIT", "100"))
PAID_TIERS = {"individual", "team"}


async def _enforce_quota(request: Request):
    """Free-tier gate. Returns a 402 response when an unpaid user has hit the
    monthly limit, else None (allowed). Fails OPEN — a billing/DB hiccup must
    never block a paying or free user mid-work.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return None  # unauthenticated requests are gated by JWTMiddleware, not metered

    try:
        sub = await get_active_subscription_by_user(user_id)
        if sub and sub.get("tier") in PAID_TIERS:
            return None  # active paid plan → unlimited
        used = await count_runs_this_month(user_id)
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("Quota check failed (allowing through): {err}", err=e)
        return None

    if used < FREE_TIER_MONTHLY_LIMIT:
        return None

    surface = "web" if "hx-request" in request.headers else "ci"
    capture_quota_exceeded(user_id=user_id, surface=surface, runs_used=used)

    if "hx-request" in request.headers:
        return templates.TemplateResponse(
            request=request,
            name="partials/upgrade_prompt.html",
            context={"limit": FREE_TIER_MONTHLY_LIMIT},
            status_code=402,
        )
    return JSONResponse(
        status_code=402,
        content={
            "error": "quota_exceeded",
            "detail": (
                f"Free tier limit of {FREE_TIER_MONTHLY_LIMIT} verifications/month "
                "reached. Upgrade to continue."
            ),
            "limit": FREE_TIER_MONTHLY_LIMIT,
        },
    )


def _resolve_surface(request: Request) -> str:
    """Which delivery surface made this call: "ci" | "mcp".

    Both the CI/CD clients and the MCP proxy POST to /api/verify/text, so the
    endpoint alone can't tell pipeline traffic from AI-agent traffic. The MCP
    server identifies itself via User-Agent (see mcp/skolem_mcp.py); anything
    else is treated as a pipeline client. Analytics-only — this never affects
    auth, quota, or the verdict, so a spoofed UA costs nothing but a mislabelled
    event.
    """
    ua = (request.headers.get("user-agent") or "").lower()
    return "mcp" if ua.startswith("skolem-mcp/") else "ci"


async def _resolve_project_id(user_id: Optional[str], project_id: Optional[str]) -> Optional[str]:
    """Return project_id only if it's a non-empty project the user actually owns;
    otherwise None. Guards against a tampered form/JSON value tagging a run onto
    another user's project. The ownership lookup only runs when a project_id is
    supplied, so the common (no-project) path stays on the fast lane."""
    if not project_id or not user_id:
        return None
    owned = await get_project(user_id, project_id)
    return project_id if owned else None


async def _get_content(upload: Optional[UploadFile], text: Optional[str], field_name: str) -> str:
    """Read an uploaded file or text string, prioritizing text if present."""
    if text and text.strip():
        return text.strip()
    
    if upload and upload.filename:
        content = await upload.read()
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"'{field_name}' exceeds maximum size of {MAX_FILE_BYTES // 1024}KB.",
            )
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail=f"'{field_name}' must be a UTF-8 encoded SQL file.",
            )
    
    raise HTTPException(status_code=400, detail=f"No valid input provided for '{field_name}'.")


def _result_to_response(result: VerificationResult) -> VerifyResponse:
    return VerifyResponse(
        status=result.status,
        divergence_reason=result.divergence_reason,
        counterexample_db=result.counterexample_db,
        query_v1_output=result.query_v1_output,
        query_v2_output=result.query_v2_output,
        error_message=result.error_message,
        explanation=result.explanation,
    )


# ---------------------------------------------------------------------------
# POST /api/verify  — Direction 1: migration equivalence
# ---------------------------------------------------------------------------

@router.post("/verify")
@limiter.limit(VERIFY_RATE_LIMIT)
async def verify_equivalence(
    request: Request,
    schema_file: Optional[UploadFile] = File(None, description="Flyway DDL file (.sql)"),
    schema_text: Optional[str] = Form(None, description="Schema text"),
    query_v1_file: Optional[UploadFile] = File(None, description="Original trusted query (.sql)"),
    query_v1_text: Optional[str] = Form(None, description="Original query text"),
    query_v2_file: Optional[UploadFile] = File(None, description="AI-generated query (.sql)"),
    query_v2_text: Optional[str] = Form(None, description="Generated query text"),
    dialect:    str = Form(default="generic", description="SQL dialect"),
    bound:      int = Form(default=DEFAULT_BOUND, ge=1, le=MAX_BOUND, description="Z3 symbolic bound"),
    timeout_ms: int = Form(default=WEB_TIMEOUT_MS, ge=1_000, le=WEB_MAX_TIMEOUT_MS),
    explain:    Optional[str] = Form(default=None, description="Set to 'true' to request AI explanation"),
    project_id: Optional[str] = Form(default=None, description="Project to attach this run to"),
):
    """
    Check whether query_v2 is semantically equivalent to query_v1
    under the constraints defined in schema_file.

    Returns a counterexample database if the queries diverge.
    """
    # Free-tier gate before any expensive work.
    gate = await _enforce_quota(request)
    if gate is not None:
        return gate

    # Read inputs, falling back to file if text is empty
    ddl_sql  = await _get_content(schema_file, schema_text, "schema_file")
    v1_sql   = await _get_content(query_v1_file, query_v1_text, "query_v1")
    v2_sql   = await _get_content(query_v2_file, query_v2_text, "query_v2")

    logger.info("Verification started | dialect={dialect} bound={bound}", dialect=dialect, bound=bound)
    start = time.monotonic()
    result = check_equivalence(
        ddl_sql=ddl_sql,
        sql_v1=v1_sql,
        sql_v2=v2_sql,
        dialect=dialect,
        bound=bound,
        timeout_ms=timeout_ms,
    )

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info("Verification done | status={status} duration={duration}ms", status=result.status, duration=duration_ms)

    # Only call LLM when the user explicitly requested an explanation
    if result.status == "divergent" and explain == "true":
        result.explanation = await explain_result(result, v1_sql, v2_sql)

    user_id = getattr(request.state, "user_id", None)
    proj_id = await _resolve_project_id(user_id, project_id)
    run_id = await save_run(result, ddl_sql, v1_sql, v2_sql, dialect, duration_ms,
                            user_id=user_id, project_id=proj_id)
    capture_verification_run(
        user_id=user_id, status=result.status, surface="web",
        duration_ms=duration_ms, bound=bound, dialect=dialect, project_id=proj_id,
    )

    if "hx-request" in request.headers:
        return templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={
                "result": result,
                "run_id": run_id,
                "bound": bound,
                "inputs": {
                    "ddl_sql": ddl_sql,
                    "sql_v1": v1_sql,
                    "sql_v2": v2_sql,
                    "dialect": dialect,
                },
            },
        )

    return _result_to_response(result)


# ---------------------------------------------------------------------------
# POST /api/explain/{run_id} — on-demand LLM explanation for a saved run
# ---------------------------------------------------------------------------

@router.post("/explain/{run_id}")
async def generate_explanation(run_id: str, request: Request):
    """
    Generate and persist an AI explanation for a previously saved divergent run.
    Returns an HTML fragment (explanation box) for htmx swap.
    """
    from db.repositories.verification_runs import get_run_by_id

    row = await get_run_by_id(run_id)
    requester_id = getattr(request.state, "user_id", None)
    if not row or row.get("user_id") != requester_id:
        raise HTTPException(status_code=404)
    if row["status"] != "divergent":
        raise HTTPException(status_code=400, detail="Explanation only available for divergent results.")

    # Return cached explanation if already generated — no duplicate LLM call
    if row.get("explanation"):
        safe = nh3.clean(markdown.markdown(row["explanation"]))
        html = f'<div class="explanation-box"><div class="explanation-label">AI Explanation</div>{safe}</div>'
        return HTMLResponse(content=html)

    result = VerificationResult(
        status=row["status"],
        divergence_reason=row["divergence_reason"],
        counterexample_db=row["counterexample_db"],
        query_v1_output=row["query_v1_output"],
        query_v2_output=row["query_v2_output"],
        error_message=row["error_message"],
    )

    capture_explanation_requested(user_id=requester_id)
    explanation = await explain_result(result, row["sql_v1"], row["sql_v2"])
    await update_explanation(run_id, explanation)

    safe_explanation = nh3.clean(markdown.markdown(explanation))
    html = f'<div class="explanation-box"><div class="explanation-label">AI Explanation</div>{safe_explanation}</div>'
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# POST /api/verify/text  — same as above but raw text body (for CI integrations)
# ---------------------------------------------------------------------------

class VerifyTextRequest(BaseModel):
    ddl_sql:    str
    sql_v1:     str
    sql_v2:     str
    dialect:    str = "generic"
    bound:      int = DEFAULT_BOUND
    timeout_ms: int = CICD_TIMEOUT_MS
    project_id: Optional[str] = None


@router.post("/verify/text", response_model=VerifyResponse)
@limiter.limit(VERIFY_RATE_LIMIT)
async def verify_equivalence_text(request: Request, body: VerifyTextRequest):
    """
    Same as POST /api/verify but accepts JSON body with raw SQL strings.
    Useful for CI/CD integrations where file uploads are inconvenient.
    """
    if body.bound > MAX_BOUND:
        raise HTTPException(
            status_code=400,
            detail=f"bound must be <= {MAX_BOUND}.",
        )

    # Free-tier gate (JSON 402) — applies to API-key clients too, so the limit
    # isn't bypassable via /api/verify/text.
    gate = await _enforce_quota(request)
    if gate is not None:
        return gate

    # Clamp to the hard ceiling rather than rejecting — a CI client asking for a
    # generous timeout should get the ceiling, not a 400.
    timeout_ms = max(1_000, min(body.timeout_ms, MAX_TIMEOUT_MS))

    start = time.monotonic()
    result = check_equivalence(
        ddl_sql=body.ddl_sql,
        sql_v1=body.sql_v1,
        sql_v2=body.sql_v2,
        dialect=body.dialect,
        bound=body.bound,
        timeout_ms=timeout_ms,
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    if result.status == "divergent":
        result.explanation = await explain_result(result, body.sql_v1, body.sql_v2)

    user_id = getattr(request.state, "user_id", None)
    proj_id = await _resolve_project_id(user_id, body.project_id)
    await save_run(result, body.ddl_sql, body.sql_v1, body.sql_v2,
                   body.dialect, duration_ms, user_id=user_id, project_id=proj_id)
    capture_verification_run(
        user_id=user_id, status=result.status, surface=_resolve_surface(request),
        duration_ms=duration_ms, bound=body.bound, dialect=body.dialect,
        project_id=proj_id,
    )

    return _result_to_response(result)


# ---------------------------------------------------------------------------
# GET /api/verify/health — liveness check for Railway
# ---------------------------------------------------------------------------

@router.get("/verify/health")
async def health():
    """Liveness check. Returns 200 if the verifier module loads correctly."""
    try:
        from z3 import Solver
        from core.equivalence import check_equivalence as _ce
        return {"status": "ok", "z3": "loaded"}
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Z3 not available: {e}")
    

@router.get("/history")
async def history(request: Request):
    user_id = getattr(request.state, "user_id", None)
    # Optional project filter from the verify-page selector (empty = all runs).
    project_id = request.query_params.get("project_id") or None
    runs = await get_recent_runs(limit=20, user_id=user_id, project_id=project_id)
    if "hx-request" in request.headers:
        return templates.TemplateResponse(
            request=request,
            name="partials/history.html",
            context={"runs": runs}
        )
    return runs

@router.get("/history/{run_id}")
async def history_detail(run_id: str, request: Request):
    from db.repositories.verification_runs import get_run_by_id
    row = await get_run_by_id(run_id)
    requester_id = getattr(request.state, "user_id", None)
    if not row or row.get("user_id") != requester_id:
        raise HTTPException(status_code=404)

    # Reconstruct a VerificationResult from the stored row
    result = VerificationResult(
        status=row["status"],
        divergence_reason=row["divergence_reason"],
        counterexample_db=row["counterexample_db"],
        query_v1_output=row["query_v1_output"],
        query_v2_output=row["query_v2_output"],
        error_message=row["error_message"],
        explanation=row["explanation"],
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/result.html",
        context={
            "result": result,
            "run_id": run_id,
            "inputs": {
                "ddl_sql": row.get("ddl_sql"),
                "sql_v1": row.get("sql_v1"),
                "sql_v2": row.get("sql_v2"),
                "dialect": row.get("dialect"),
            },
        },
    )

# ---------------------------------------------------------------------------
# POST /api/verify/extract-constraints — Parse DDL and preview schema
# ---------------------------------------------------------------------------
@router.post("/verify/extract-constraints")
async def extract_constraints(
    request: Request,
    schema_file: Optional[UploadFile] = File(None),
    schema_text: str = Form(""),
    dialect: str = Form("generic"),
):
    from core.ddl_parser import parse_ddl
    
    try:
        sql = ""
        if schema_text and schema_text.strip():
            sql = schema_text.strip()
        elif schema_file and schema_file.filename:
            content = await schema_file.read()
            sql = content.decode("utf-8")
            
        if not sql.strip():
            return templates.TemplateResponse(
                request=request,
                name="partials/schema_summary.html",
                context={"schema": None, "error": None}
            )
            
        schema = parse_ddl(sql, dialect=dialect)
        return templates.TemplateResponse(
            request=request,
            name="partials/schema_summary.html",
            context={"schema": schema, "error": None}
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/schema_summary.html",
            context={"schema": None, "error": str(e)}
        )