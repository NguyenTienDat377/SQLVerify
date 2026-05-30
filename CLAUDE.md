# CLAUDE.md — SQLVerify

A context file for Claude Code. Read this fully before touching any file.

---

## What SQLVerify is

A formal verification tool for AI-generated SQL. Given a Flyway DDL schema and two SQL queries (original vs AI-rewritten), SQLVerify uses Z3/SMT solving to either **prove they are semantically equivalent** or **produce a concrete counterexample database** where they diverge.

**Core value proposition:** Deterministic formal verification as an antidote to probabilistic AI output — proof that a query does what it's intended to do, not just that it runs.

**Target users (V1):** Backend engineers reviewing AI-generated SQL before it ships to production.  
**Target users (V2):** AI agent pipelines that need automated SQL validation with minimal human review.  
**Market timing:** Positioned for 2026 as LLM-in-production adoption matures and SQL agent use grows.

---

## Stack

- **Backend:** FastAPI (async)
- **Solver:** Z3 (via `z3-solver` Python package)
- **SQL parsing:** sqlglot
- **Database:** Supabase (managed PostgreSQL)
- **Frontend:** Jinja2 templates + HTMX
- **LLM explainer:** Multi-provider abstraction (Claude / Gemini / GPT, swappable via env var)
- **Deployment target:** Render (always-on container, NOT serverless Lambda — Z3 cold starts are too heavy)

---

## Directory structure

```
SQLVerify/
├── main.py                         # FastAPI app entry point
├── config.py                       # ✅ DONE — Pydantic Settings (reads .env)
├── CLAUDE.md                       # This file
├── .env                            # Never commit — secrets live here
├── .env.example                    # Commit this — placeholder keys only
├── .gitignore
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── models.py                   # ✅ DONE — dataclasses: Column, Table, SchemaModel, VerificationResult
│   ├── ddl_parser.py               # ✅ DONE — Flyway DDL → SchemaModel via sqlglot
│   ├── sql_encoder.py              # ✅ DONE — SELECT + SchemaModel → Z3 QueryFormula
│   ├── equivalence.py              # ✅ DONE — two QueryFormulas → VerificationResult with counterexample
│   ├── constraint_check.py         # ⬜ STUB — empty placeholder for Direction 2 (single-query constraint check)
│   └── witness.py                  # ⬜ STUB — empty placeholder for counterexample witness generation
│
├── explainer/
│   ├── __init__.py                 # ✅ DONE — empty
│   ├── explain.py                  # ✅ DONE — explain_result() public API: VerificationResult → plain-English string
│   ├── providers.py                # ✅ DONE — LLMProvider ABC + Claude/Gemini/GPT implementations + get_provider()
│   └── prompts.py                  # ✅ DONE — EQUIVALENCE_EXPLANATION and CONSTRAINT_EXPLANATION prompt templates
│
├── api/
│   ├── __init__.py
│   └── verify.py                   # ✅ DONE — all endpoints (see "What is DONE > api/" below)
│
├── db/
│   ├── __init__.py
│   ├── client.py                   # ✅ DONE — Supabase client (uses service key)
│   └── repositories/
│       ├── __init__.py
│       └── verification_runs.py    # ✅ DONE — save_run(), get_recent_runs(), get_run_by_id(), update_explanation()
│
├── auth/                           # ❌ NOT BUILT — Supabase auth middleware
│   └── __init__.py
│
└── web/
    ├── static/
    │   └── css/
    │       └── style.css
    └── templates/
        ├── base.html               # ✅ DONE
        ├── verify.html             # ✅ DONE — SQL Input tab + History tab (HTMX wired)
        └── partials/
            ├── result.html         # ✅ DONE — HTMX partial: equivalent/divergent/error/unknown badges + counterexample table + Explain button
            └── history.html        # ✅ DONE — HTMX partial: list of past runs, click to replay result

---

## What is DONE

### core/
- `models.py` — `Column`, `ForeignKey`, `Table`, `SchemaModel`, `VerificationResult` dataclasses
- `ddl_parser.py` — parses Flyway-style DDL SQL into `SchemaModel` using sqlglot
- `sql_encoder.py` — encodes a SELECT query + SchemaModel into Z3 `QueryFormula`
- `equivalence.py` — takes two `QueryFormula` objects, runs Z3, returns `VerificationResult`

### explainer/
- `prompts.py` — two prompt templates:
  - `EQUIVALENCE_EXPLANATION` — explains why two queries diverge, with counterexample
  - `CONSTRAINT_EXPLANATION` — explains why a query violates a constraint property
- `providers.py` — `LLMProvider` ABC with single method `async def explain(prompt: str) -> str`, plus:
  - `AnthropicProvider` (Claude)
  - `OpenAIProvider` (GPT)
  - `GoogleProvider` (Gemini)
  - `get_provider()` factory — reads `EXPLAINER_PROVIDER` env var, returns right instance
- `explain.py` — `explain_result(result, sql_v1, sql_v2, provider_name=None) -> str`: public async function that formats the prompt and calls the provider. Returns `""` for non-divergent results. Catches `ExplainerError` and returns a fallback string — never breaks the caller.

### api/
- `verify.py` — all endpoints:
  - `POST /api/verify` — multipart form (`schema_file` + `query_v1` + `query_v2` + optional `explain=true`), HTMX-aware. Calls `explain_result()` only when `explain=true` and status is divergent.
  - `POST /api/explain/{run_id}` — on-demand LLM explanation for a saved run. Fetches from Supabase, calls `explain_result()`, persists via `update_explanation()`, returns HTML fragment for HTMX swap. Returns cached explanation if already generated.
  - `POST /api/verify/text` — JSON body for CI/CD pipelines (always calls explainer on divergent results)
  - `GET /api/history` — returns `history.html` partial with list of past runs
  - `GET /api/history/{run_id}` — returns `result.html` partial replaying a specific run
  - `GET /api/verify/health` — liveness check

### db/
- `client.py` — Supabase client using service key
- `repositories/verification_runs.py` — `save_run()`, `get_recent_runs()`, `get_run_by_id()`, `update_explanation()`
- Supabase `verification_runs` table is set up with RLS enabled

### web/
- `verify.html` — two-tab layout: SQL Input (form) + History (HTMX-loaded). `switchTab()` JS function handles tab switching.
- `partials/result.html` — renders `VerificationResult` with status badge + counterexample table + divergence reason
- `partials/history.html` — renders list of past runs with timestamp, status badge, query preview, and "View" button

---

## What is NOT BUILT (next tasks)

### 1. `auth/` — Supabase auth middleware ← BUILD THIS NEXT
Not started. Low priority for V1 demo.

### 2. `core/constraint_check.py` — Direction 2: single-query constraint checking
File exists but is empty. Intended to check whether a single query satisfies schema constraints (FK, PK, NOT NULL). Not required for V1 equivalence demo.

### 3. `core/witness.py` — Witness/counterexample generation helpers
File exists but is empty. Intended to clean up and format Z3 model output into human-readable counterexample databases. Currently handled inline in `equivalence.py`.

### 4. Tests
No test suite yet. When writing tests, cover:
- `core/ddl_parser.py` — DDL parsing edge cases
- `core/equivalence.py` — known equivalent and known divergent query pairs
- `api/verify.py` — endpoint smoke tests

---

## Key design decisions (do not change without reason)

| Decision | Rationale |
|---|---|
| `bound=3` hardcoded in `equivalence.py` | Catches >95% of real SQL semantic bugs. Not exposed in UI — would confuse engineers. Power users can override via env var. |
| V1 scope: single JOIN only (INNER or LEFT) | Keeps Z3 encoding tractable. No CTEs, window functions, subqueries, UNION, RIGHT/FULL OUTER JOIN. |
| HTMX for frontend interactivity | No React build step, no npm. Jinja2 + HTMX keeps the stack simple and server-rendered. |
| Multi-LLM provider abstraction | Factory pattern in `explainer/providers.py`. Adding a new provider = one new class + one line in `get_provider()`. Never add provider-specific logic outside `providers.py`. |
| Explainer is on-demand only | The "Explain" button is explicit user action. Never auto-call LLM on every verification — cost and latency. |
| Supabase for DB + auth | Managed PostgreSQL + built-in auth. No self-hosted infra. Service key stays server-side only, never exposed to frontend. |
| Render for deployment | Always-on container. NOT Lambda/serverless — Z3 binary is too large for cold starts. |
| No cross-dialect comparison in V1 | sqlglot supports dialects but encoding dialect-specific semantics in Z3 is non-trivial. Out of scope. |

---

## V1 known limitations

- Single JOIN per query only (INNER or LEFT). No RIGHT, FULL OUTER.
- No CTEs (`WITH` clauses)
- No window functions (`ROW_NUMBER`, `RANK`, etc.)
- No subqueries or `UNION`
- NULLs ARE modeled (each cell is a `(is_null, value)` pair, three-valued logic per the VeriEQL paper): `IS NULL` / `IS NOT NULL`, `COUNT(col)` vs `COUNT(*)`, and LEFT JOIN null-extension are all encoded. NOT NULL / PK columns forced non-NULL; FK columns may be NULL.
- WHERE/HAVING predicates compare a column to a *literal* only — column-to-column predicates (e.g. `WHERE b = a`) are silently dropped
- TEXT/TIMESTAMP equality is symbolic (interned to int) — string ordering not supported
- CHECK constraints parsed but not encoded into Z3 (FK/PK/NOT NULL cover most real bugs)
- Equivalence is checked under **bag semantics** via tuple multiplicity (paper Eqns 1–2); no list/ORDER BY semantics
- GROUP BY merges rows with equal keys (Dedup); group keys are assumed to come from the FROM table
- Integer value domain is a finite window `[-bound*4, bound*4]` (includes 0/negatives) — an "equivalent" verdict is sound only within this window
- `bound` (default 5) may miss bugs requiring more row interactions (rare in practice)
- No cross-dialect comparison (e.g. PostgreSQL vs MySQL semantics)

---

## Environment variables

```

ANTHROPIC_API_KEY=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_KEY=
EXPLAINER_PROVIDER=claude # claude | openai | google

````

All of these go into Render's environment variable panel. Never in the repo.

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
````

Open `http://localhost:8000`.

---

## Supabase schema

```sql
CREATE TABLE verification_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status          TEXT NOT NULL,          -- equivalent | divergent | unknown | error
    divergence_reason TEXT,
    counterexample_db JSONB,
    query_v1_output JSONB,
    query_v2_output JSONB,
    error_message   TEXT,
    explanation     TEXT,                   -- filled in by /api/explain
    ddl_input       TEXT,
    query_a_input   TEXT,
    query_b_input   TEXT,
    result_json     JSONB,
    bound           INT DEFAULT 3,
    duration_ms     INT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

RLS is enabled. The service key in `db/client.py` bypasses RLS for server-side writes.
