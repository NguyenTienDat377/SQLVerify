# CLAUDE.md — SQLVerify

A context file for Claude Code. Read this fully before touching any file.

---

## What SQLVerify is

A formal verification tool for AI-generated SQL. Given a Flyway DDL schema and two SQL queries (original vs AI-rewritten), SQLVerify uses Z3/SMT solving to either **prove they are semantically equivalent** or **produce a concrete counterexample database** where they diverge.

**Core value proposition:** Deterministic formal verification as an antidote to probabilistic AI output — proof that a query does what it's intended to do, not just that it runs.

**Two delivery surfaces (same core engine):**

- **Web tool** — backend engineers reviewing AI-generated SQL before it ships, via the HTMX UI (`POST /api/verify`, on-demand "Explain" button).
- **CI/CD tool** — automated SQL validation in pipelines and AI-agent loops, via the JSON endpoint (`POST /api/verify/text`), which always explains divergent results.

**Future function** - Check the queries if it fits business input via a box of users' expected

**Pricing:** Freemium — the supported SQL subset is deliberately wide (real multi-table-join queries verify, not just two-table demos) to grow the top of the funnel; anything outside the subset stays fail-closed.
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
`tests/smoke_test.py` exists — 28 end-to-end checks over `check_equivalence()`:
known equivalent/divergent pairs (incl. LEFT/RIGHT JOIN ON-vs-WHERE cases) and
fail-closed rejection of unsupported constructs. Run with
`.venv/bin/python tests/smoke_test.py` (no pytest required, but pytest-compatible).

`tests/paper_cases_test.py` — 18 regression tests derived from the VeriEQL
paper (`3649849.pdf`): IC-PK composite keys, IC-FK/IC-NN-dependent
equivalences, three-valued logic, NULL aggregation, GROUP BY NULL-key Dedup,
bag multiplicity. Same runner style as the smoke test.

`tests/differential_test.py` — differential fuzzing: random V1-subset query
pairs verified by Z3, then cross-checked against concrete SQLite execution
(every "equivalent" verdict attacked with sampled databases within the bound;
every "divergent" witness replayed). Run with
`.venv/bin/python tests/differential_test.py [--seeds N --bound B]`.

`docs/encoding_audit.md` — rule-by-rule audit of the Z3 encoding against the
paper (Figs 8–12, Eqns 1–2), with verdicts and the audit campaign results.

Still missing:
- `core/ddl_parser.py` — DDL parsing edge cases
- `api/verify.py` — endpoint smoke tests

---

## Key design decisions (do not change without reason)

| Decision | Rationale |
|---|---|
| `bound=3` hardcoded in `equivalence.py` | Catches >95% of real SQL semantic bugs. Not exposed in UI — would confuse engineers. Power users can override via env var. |
| Join scope: any number of INNER joins, or one LEFT/RIGHT join | INNER joins are encoded as an N-table "join bag" (one entry per row combination across the joined tables); for INNER joins ON ≡ WHERE, so there is no null-extension subtlety. A single LEFT/RIGHT join uses a dedicated outer-join path where ON and WHERE are encoded separately (the distinction changes outer-join results). An outer join combined with any other join is rejected — multi-table joins must be INNER. Still no CTEs, window functions, subqueries, UNION, FULL OUTER, CROSS, or self-joins. |
| Fail-closed parsing/encoding | Any SQL construct outside the supported subset raises `ValueError` (→ status `error`) instead of being silently dropped. A dropped predicate or SELECT expression weakens the encoding and can produce a false "equivalent" — the one failure mode a verifier must not have. Never "skip" unsupported syntax. |
| Witness cross-check in `equivalence.py` | After Z3 finds a divergence, both queries run on the SQLite witness; if their outputs agree, the verdict is downgraded to `error` (encoder bug) instead of showing a fake counterexample. |
| HTMX for frontend interactivity | No React build step, no npm. Jinja2 + HTMX keeps the stack simple and server-rendered. |
| Multi-LLM provider abstraction | Factory pattern in `explainer/providers.py`. Adding a new provider = one new class + one line in `get_provider()`. Never add provider-specific logic outside `providers.py`. |
| Explainer is on-demand only | The "Explain" button is explicit user action. Never auto-call LLM on every verification — cost and latency. |
| Supabase for DB + auth | Managed PostgreSQL + built-in auth. No self-hosted infra. Service key stays server-side only, never exposed to frontend. |
| Render for deployment | Always-on container. NOT Lambda/serverless — Z3 binary is too large for cold starts. |
| No cross-dialect comparison in V1 | sqlglot supports dialects but encoding dialect-specific semantics in Z3 is non-trivial. Out of scope. |

---

## V1 known limitations

Everything outside the supported subset is **rejected with a clear error** (fail-closed), never silently ignored.

- Any number of INNER joins per query, OR exactly one LEFT/RIGHT join. An outer join cannot be combined with another join (multi-table joins must be INNER). No FULL OUTER, CROSS, or self-joins. Each JOIN ON must be a single column equality. Note: the INNER join bag is `bound^n` over n tables, so deep chains (n≥4) get slower and may return `unknown` on timeout — the bound is never silently lowered.
- No CTEs (`WITH` clauses)
- No window functions (`ROW_NUMBER`, `RANK`, etc.)
- No subqueries, `UNION`, `SELECT DISTINCT`, `SELECT *`, `LIMIT`/`OFFSET`
- No `OR` / `IN` / `BETWEEN` / `LIKE` in WHERE/HAVING — AND-chains of comparisons and `IS [NOT] NULL` only
- NULLs ARE modeled (each cell is a `(is_null, value)` pair, three-valued logic per the VeriEQL paper): `IS NULL` / `IS NOT NULL`, `COUNT(col)` vs `COUNT(*)`, and LEFT/RIGHT JOIN null-extension are all encoded. NOT NULL / PK columns forced non-NULL; FK columns may be NULL.
- WHERE/HAVING predicates: column vs literal AND column vs column (e.g. `WHERE b = a`) are both encoded under three-valued logic
- ON vs WHERE is encoded faithfully for outer joins: a right-table filter in WHERE makes LEFT JOIN ≡ INNER JOIN; `WHERE right.col IS NULL` keeps the anti-join idiom
- TEXT/TIMESTAMP equality is symbolic (globally interned to int) — ordering comparisons (`>`, `<`) on TEXT/TIMESTAMP are rejected
- Boolean literals in predicates (`WHERE active = TRUE`) are rejected
- CHECK constraints parsed but not encoded into Z3 (FK/PK/NOT NULL cover most real bugs)
- Equivalence is checked under **bag semantics** via tuple multiplicity (paper Eqns 1–2); no list semantics — `ORDER BY` is accepted but ignored
- GROUP BY merges rows with equal keys (Dedup). For INNER (and no-join) queries, group keys and bare SELECT columns may come from any joined table. A bare SELECT column must still appear in GROUP BY (ambiguous projections — invalid in standard SQL — are rejected). The single LEFT/RIGHT outer-join path keeps the older restriction: group keys must come from the FROM table, and RIGHT JOIN + GROUP BY requires non-nullable group keys.
- HAVING supports aggregate comparisons; the aggregate does NOT need to appear in the SELECT list
- Integer value domain is a finite window `[-(bound*4 + max|literal|), bound*4 + max|literal|]` — automatically widened to cover every numeric literal in the queries; an "equivalent" verdict is sound only within this window
- `bound` (default 3) may miss bugs requiring more row interactions (rare in practice)
- No cross-dialect comparison (e.g. PostgreSQL vs MySQL semantics)
- On a `divergent` verdict, both queries are re-run on the SQLite witness; if outputs agree the run is reported as `error` (internal encoding bug) rather than trusting Z3

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

## Migration #1

```sql
ALTER TABLE public.verification_runs
    ADD COLUMN IF NOT EXISTS user_id UUID
        REFERENCES auth.users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS verification_runs_user_id_idx
    ON public.verification_runs (user_id);

─────────
DROP POLICY IF EXISTS "Allow all"                       ON public.verification_runs;
DROP POLICY IF EXISTS "Enable read access for all users" ON public.verification_runs;
DROP POLICY IF EXISTS "Enable insert for all users"      ON public.verification_runs;


CREATE POLICY "users_select_own_runs"
    ON public.verification_runs
    FOR SELECT
    TO authenticated
    USING (user_id = auth.uid());


CREATE POLICY "users_insert_own_runs"
    ON public.verification_runs
    FOR INSERT
    TO authenticated
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_update_own_runs"
    ON public.verification_runs
    FOR UPDATE
    TO authenticated
    USING  (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());
──────────────────────────────────────
GRANT SELECT, INSERT, UPDATE
    ON public.verification_runs
    TO authenticated;

```

RLS is enabled. The service key in `db/client.py` bypasses RLS for server-side writes.
