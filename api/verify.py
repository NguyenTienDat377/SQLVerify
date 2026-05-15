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

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

from core.equivalence import check_equivalence
from core.models import VerificationResult

from explainer.explain import explain_result

import time
from db.repositories.verification_runs import save_run


templates = Jinja2Templates(directory="web/templates")

router = APIRouter(prefix="/api", tags=["verify"])


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 512 * 1024  # 512 KB per file — SQL files are never bigger
MAX_BOUND = 6


async def _read_file(upload: UploadFile, field_name: str) -> str:
    """Read an uploaded file into a string with size guard."""
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


def _result_to_response(result: VerificationResult) -> VerifyResponse:
    return VerifyResponse(
        status=result.status,
        divergence_reason=result.divergence_reason,
        counterexample_db=result.counterexample_db,
        query_v1_output=result.query_v1_output,
        query_v2_output=result.query_v2_output,
        error_message=result.error_message,
    )


# ---------------------------------------------------------------------------
# POST /api/verify  — Direction 1: migration equivalence
# ---------------------------------------------------------------------------

@router.post("/verify")
async def verify_equivalence(
    request: Request,
    schema_file: UploadFile = File(..., description="Flyway DDL file (.sql)"),
    query_v1:   UploadFile = File(..., description="Original trusted query (.sql)"),
    query_v2:   UploadFile = File(..., description="AI-generated query (.sql)"),
    dialect:    str = Form(default="generic", description="SQL dialect"),
    bound:      int = Form(default=3, ge=1, le=MAX_BOUND, description="Z3 symbolic bound"),
    timeout_ms: int = Form(default=15_000, ge=1_000, le=60_000),
):
    """
    Check whether query_v2 is semantically equivalent to query_v1
    under the constraints defined in schema_file.

    Returns a counterexample database if the queries diverge.
    """
    # Read all three files
    ddl_sql  = await _read_file(schema_file, "schema_file")
    v1_sql   = await _read_file(query_v1, "query_v1")
    v2_sql   = await _read_file(query_v2, "query_v2")

    # Validate inputs are non-empty
    for name, content in [("schema_file", ddl_sql), ("query_v1", v1_sql), ("query_v2", v2_sql)]:
        if not content.strip():
            raise HTTPException(status_code=400, detail=f"'{name}' is empty.")

    start = time.monotonic()
    # Run verification (all errors caught inside check_equivalence)
    result = check_equivalence(
        ddl_sql=ddl_sql,
        sql_v1=v1_sql,
        sql_v2=v2_sql,
        dialect=dialect,
        bound=bound,
        timeout_ms=timeout_ms,
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    # Generate LLM explanation for divergent results
    if result.status == "divergent":
        result.explanation = await explain_result(result, v1_sql, v2_sql)

    await save_run(result, ddl_sql, v1_sql, v2_sql, dialect, duration_ms)

    if "hx-request" in request.headers:
        return templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={"result": result}
        )

    return _result_to_response(result)


# ---------------------------------------------------------------------------
# POST /api/verify/text  — same as above but raw text body (for CI integrations)
# ---------------------------------------------------------------------------

class VerifyTextRequest(BaseModel):
    ddl_sql:    str
    sql_v1:     str
    sql_v2:     str
    dialect:    str = "generic"
    bound:      int = 3
    timeout_ms: int = 15_000


@router.post("/verify/text", response_model=VerifyResponse)
async def verify_equivalence_text(body: VerifyTextRequest):
    """
    Same as POST /api/verify but accepts JSON body with raw SQL strings.
    Useful for CI/CD integrations where file uploads are inconvenient.
    """
    if body.bound > MAX_BOUND:
        raise HTTPException(
            status_code=400,
            detail=f"bound must be <= {MAX_BOUND}.",
        )

    start = time.monotonic()
    result = check_equivalence(
        ddl_sql=body.ddl_sql,
        sql_v1=body.sql_v1,
        sql_v2=body.sql_v2,
        dialect=body.dialect,
        bound=body.bound,
        timeout_ms=body.timeout_ms,
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    if result.status == "divergent":
        result.explanation = await explain_result(result, body.sql_v1, body.sql_v2)

    await save_run(result, ddl_sql, v1_sql, v2_sql, dialect, duration_ms)

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