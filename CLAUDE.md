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
├── main.py                         # FastAPI app entry point — routers, pinned CORS, rate-limit + JWT middleware
│                                   #   (config is read via os.getenv per-module; there is no config.py)
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
│   ├── explain.py                  # ✅ DONE — explain_result() public API (guarded by the circuit breaker)
│   ├── providers.py                # ✅ DONE — LLMProvider ABC + Claude/Gemini/GPT (httpx errors → ExplainerError)
│   ├── circuit_breaker.py          # ✅ DONE — async circuit breaker: fail fast when the LLM provider is down
│   └── prompts.py                  # ✅ DONE — EQUIVALENCE_EXPLANATION and CONSTRAINT_EXPLANATION prompt templates
│
├── api/
│   ├── __init__.py
│   ├── verify.py                   # ✅ DONE — all endpoints (see "What is DONE > api/" below)
│   ├── auth.py                     # ✅ DONE — GitHub OAuth + magic-link + session cookies + logout (POST)
│   ├── keys.py                     # ✅ DONE — per-user API key CRUD (HTMX partials)
│   ├── projects.py                 # ✅ DONE — per-user project CRUD (HTMX partials)
│   ├── billing.py                  # ✅ DONE — Lemon Squeezy checkout + customer portal redirects
│   └── webhooks.py                 # ✅ DONE — Lemon Squeezy subscription webhooks
│
├── db/
│   ├── __init__.py
│   ├── client.py                   # ✅ DONE — Supabase client (uses service key)
│   └── repositories/
│       ├── __init__.py
│       ├── verification_runs.py    # ✅ DONE — save_run(), get_recent_runs(), get_run_by_id(), update_explanation(), count_runs_this_month() (save_run/get_recent_runs take project_id)
│       ├── subscriptions.py        # ✅ DONE — upsert_subscription(), get_active_subscription(), get_active_subscription_by_user()
│       ├── api_keys.py             # ✅ DONE — create/list/revoke/resolve per-user API keys (sha256-hashed)
│       └── projects.py             # ✅ DONE — create/list/get/delete per-user projects (owner-scoped)
│
├── auth/                           # ✅ DONE — JWTMiddleware (Supabase JWKS) + per-user API-key path
│   └── middleware.py
│
└── web/
    ├── static/
    │   └── css/
    │       └── styles.css
    └── templates/
        ├── base.html               # ✅ DONE — app shell + topbar (Projects / API Keys / Billing / Sign out) + Terms/Privacy footer ({% block title %})
        ├── landing.html            # ✅ DONE — marketing page + GitHub + magic-link sign-in
        ├── verify.html             # ✅ DONE — project selector + SQL Input tab + History tab (HTMX wired)
        ├── pricing.html            # ✅ DONE — plans; CTAs → /billing/checkout
        ├── keys.html               # ✅ DONE — per-user API key management
        ├── projects.html           # ✅ DONE — per-user project management (create/list/delete)
        ├── terms.html              # ✅ DONE — Terms of Service (public, no auth)
        ├── privacy.html            # ✅ DONE — Privacy Policy (public, no auth)
        └── partials/
            ├── result.html         # ✅ DONE — equivalent/divergent/error/unknown badges + counterexample + Explain
            ├── history.html        # ✅ DONE — list of past runs, click to replay
            ├── api_keys.html        # ✅ DONE — API key list + show-once raw key banner
            ├── projects.html        # ✅ DONE — project list + delete + inline error/empty states
            └── upgrade_prompt.html # ✅ DONE — 402 free-tier upgrade prompt (HTMX)

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
- `explain.py` — `explain_result(result, sql_v1, sql_v2, provider_name=None) -> str`: public async function that formats the prompt and calls the provider **through a shared circuit breaker**. Returns `""` for non-divergent results. Catches `ExplainerError`/open-circuit and returns a fallback string — never breaks the caller.
- `circuit_breaker.py` — `CircuitBreaker`: a per-process async 3-state breaker (closed→open→half_open). After `EXPLAINER_BREAKER_THRESHOLD` (default 3) consecutive failures it short-circuits for `EXPLAINER_BREAKER_RESET_S` (default 30s), so an LLM outage stops burning credits/latency instead of retrying. Providers wrap `httpx` errors as `ExplainerError` so an outage degrades gracefully (a latent crash that pre-dated this).

### api/
- `verify.py` — verification endpoints, each per-IP rate-limited (`VERIFY_RATE_LIMIT`,
  default 30/min) and free-tier quota-gated (`_enforce_quota`, fails open):
  - `POST /api/verify` — multipart form (`schema_file` + `query_v1` + `query_v2` + optional `explain=true`, optional `project_id`), HTMX-aware. Web timeout default 15s (cap 60s). Calls `explain_result()` only when `explain=true` and status is divergent. Attaches `user_id` and an **ownership-validated** `project_id` (`_resolve_project_id` drops a foreign/tampered id) to the saved run.
  - `POST /api/explain/{run_id}` — on-demand LLM explanation for a saved run. **Ownership-checked** (`row.user_id != requester → 404`). Fetches, calls `explain_result()`, persists, returns HTML fragment. Returns cached explanation if present.
  - `POST /api/verify/text` — JSON body for CI/CD pipelines (optional `project_id`). CI timeout default 60s (clamped to 120s). Always explains divergent results; attaches `user_id` + ownership-validated `project_id`; quota-gated (JSON 402).
  - `GET /api/history` — `history.html` partial, scoped to the requester's `user_id`; optional `project_id` query param narrows to one project (empty = all runs).
  - `GET /api/history/{run_id}` — `result.html` partial replaying a run; **ownership-checked** (404 on mismatch).
  - `GET /api/verify/health` — liveness check
- `auth.py` — `/auth/login` (GitHub OAuth), `/auth/magic-link` (Supabase OTP email),
  `/auth/callback` + `/auth/set-session` (implicit-flow tokens → HttpOnly cookies),
  `/auth/logout` (**POST**, clears cookies).
- `keys.py` — `/api/keys` create/list + `/api/keys/{id}/revoke` (session-protected, HTMX).
- `projects.py` — `/api/projects` create/list + `/api/projects/{id}/delete` (session-protected, HTMX); all owner-scoped.
- `billing.py` — `/billing/checkout?plan=…` (LS buy-link + `user_id` custom data) and
  `/billing/portal` (fresh signed LS portal URL via the LS API).
- `webhooks.py` — `POST /api/webhooks/lemonsqueezy` (HMAC-verified) → `upsert_subscription`,
  handling `subscription_created/updated/cancelled/expired/resumed/payment_failed`.

### auth/
- `middleware.py` — `JWTMiddleware`: authenticates every non-public request via **two
  paths** and injects `request.state.user_id` (+ `user_email`, `auth_method`):
  - browser session — Supabase JWT (`sb-access-token` cookie or `Bearer <jwt>`),
    verified against Supabase JWKS with issuer + audience checks;
  - CI/API client — per-user API key (`Bearer sqv_…` or `X-API-Key`), resolved via the
    `api_keys` table with a ~60s per-process lookup cache.

### db/
- `client.py` — Supabase client using the service key (bypasses RLS; repos scope by `user_id` in code)
- `repositories/verification_runs.py` — `save_run()`, `get_recent_runs()`, `get_run_by_id()`, `update_explanation()`, `count_runs_this_month()` (`save_run`/`get_recent_runs` accept `project_id`)
- `repositories/subscriptions.py` — `upsert_subscription()`, `get_active_subscription()`, `get_active_subscription_by_user()`
- `repositories/api_keys.py` — `create/list/revoke/resolve` per-user API keys (sha256-hashed, shown once)
- `repositories/projects.py` — `create/list/get/delete` per-user projects, all scoped by `owner_id` (the `get_project` ownership check also backs run tagging)
- Supabase tables `verification_runs`, `subscriptions`, `api_keys`, `projects` with RLS enabled
  (Migrations #1–#4 below; the service key intentionally bypasses RLS for server writes)

### web/
- `landing.html` — marketing page; GitHub sign-in + magic-link email form (HTMX)
- `verify.html` — project selector (`#project-select`) + two-tab layout: SQL Input (form) + History (HTMX-loaded). `switchTab()` JS handles tabs; the form and History tab `hx-include` the selector so a run is tagged with the chosen project and History filters by it.
- `pricing.html` — plan cards; CTAs point at `/billing/checkout?plan=…`
- `keys.html` — API key management (create form + list)
- `projects.html` — project management (create form + list + delete)
- `terms.html` / `privacy.html` — legal pages, served by public `GET /terms` and `GET /privacy` in `main.py` (in the `JWTMiddleware` public allowlist); linked from the `base.html` footer
- `partials/result.html` — `VerificationResult`: status badge + counterexample table + divergence reason
- `partials/history.html` — past runs with timestamp, status badge, query preview, "View"
- `partials/api_keys.html` — key list + the show-once raw-key banner
- `partials/projects.html` — project list + delete buttons + inline error / empty states
- `partials/upgrade_prompt.html` — 402 free-tier upgrade prompt
web/templates/404.html: A custom 404 page that tells the user the page couldn't be found, with a button to return home.
web/templates/500.html: A custom 500 page indicating an internal server error, also with a button to return home.

---

## What is NOT BUILT (next tasks)

### 1. `core/constraint_check.py` — Direction 2: single-query constraint checking
File exists but is empty. Intended to check whether a single query satisfies schema constraints (FK, PK, NOT NULL). Not required for the equivalence engine.

### 2. `core/witness.py` — Witness/counterexample generation helpers
File exists but is empty. Intended to clean up and format Z3 model output into human-readable counterexample databases. Currently handled inline in `equivalence.py`.

### 3. Scaling (memory: scaling-architecture-roadmap)
Async job queue + competing consumers (Postgres `SKIP LOCKED`), result cache,
shared-state rate-limiter/breaker, poison-job watchdog. Not started — see the
scaling roadmap memory.

---

## Tests

All suites are standalone (no pytest required, but pytest-compatible):
`.venv/bin/python tests/<file>.py`.

- `tests/smoke_test.py` — 32 end-to-end checks over `check_equivalence()`:
  equivalent/divergent pairs (incl. LEFT/RIGHT ON-vs-WHERE, multi-table INNER
  joins, join reordering, GROUP BY on a joined column) and fail-closed rejection
  of unsupported constructs.
- `tests/paper_cases_test.py` — 21 regression tests from the VeriEQL paper
  (`docs/references/veriEQL-2024.pdf`): IC-PK composite keys, IC-FK/IC-NN
  equivalences, three-valued logic, NULL aggregation, GROUP BY NULL-key Dedup,
  bag multiplicity, plus multi-table-join chains.
- `tests/differential_test.py` — differential fuzzing: random query pairs (incl.
  3-table INNER chains) verified by Z3, then cross-checked against concrete
  SQLite (every "equivalent" attacked with sampled DBs within the bound; every
  "divergent" witness replayed). `--seeds N --bound B`.
- `tests/api_verify_text_test.py` — `/api/verify/text` end-to-end (timeout
  defaults/clamp, persistence, divergent explain) with Supabase/LLM stubbed.
- `tests/api_keys_test.py` — key hashing + the dual auth path (session JWT vs
  `sqv_` API key) + magic-link OTP call.
- `tests/circuit_breaker_test.py` — the explainer circuit breaker (trips, short-
  circuits, half-open recovery).
- `tests/billing_test.py` — free-tier quota gate, checkout redirect, portal,
  webhook `payment_failed`.
- `docs/encoding_audit.md` — rule-by-rule audit of the Z3 encoding vs the paper
  (Figs 8–12, Eqns 1–2).

Still missing: `core/ddl_parser.py` edge cases.

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
| No cross-dialect comparison | sqlglot supports dialects but encoding dialect-specific semantics in Z3 is non-trivial. Out of scope. |
| Per-surface Z3 timeouts | Web `POST /api/verify` defaults 15s (cap 60s) for a snappy UI; CI `POST /api/verify/text` defaults 60s (clamped to 120s) to favour a verdict over latency. On timeout the result is `unknown` — never a wrong answer. |
| Per-IP rate limiting (slowapi) | The two solve endpoints are `@limiter.limit`-decorated (default 30/min). Verification is expensive, so an unthrottled endpoint lets one client pin a worker. The `limiter` lives in `api/verify.py`; it is attached to the app + given its 429 handler in `main.py`. |
| Circuit breaker on LLM calls | `explainer/circuit_breaker.py` fails fast when the provider is down so an outage doesn't burn credits/latency. Per-process state. |
| Free-tier quota fails **open** | `_enforce_quota` returns the request through on any billing/DB lookup error — a metering hiccup must never block a paying or free user mid-work. |
| Two auth paths, one `user_id` | Session JWT (browser) **or** per-user API key (CI), both resolved by `JWTMiddleware` into `request.state.user_id`. Access control is enforced in app code (ownership checks + repo `user_id` scoping), **not** RLS — the service key bypasses RLS. |
| Billing via Lemon Squeezy (MoR) | Merchant of Record handles global VAT; we never touch card data. Plan/quota resolve by `user_id`, stamped onto the subscription via LS checkout custom data. Rate-limiter / breaker / API-key cache are per-process — move to shared state when scaling to multiple workers. |

---

## Known limitations (supported SQL subset)

Everything outside the supported subset is **rejected with a clear error** (fail-closed), never silently ignored. The subset is widening over time (see the SQL-subset-expansion roadmap memory); anything not yet supported stays fail-closed.

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
SITE_URL=                 # full origin, e.g. https://sqlverify.com (no trailing slash) — used by auth redirects + CORS

# Billing (Lemon Squeezy)
LEMONSQUEEZY_WEBHOOK_SECRET=   # Settings → Webhooks (HMAC signing secret)
LEMONSQUEEZY_API_KEY=          # for the customer portal lookup
LS_INDIVIDUAL_VARIANT_ID=      # numeric variant id (webhook → tier map)
LS_TEAM_VARIANT_ID=
LS_INDIVIDUAL_CHECKOUT_URL=    # the .../checkout/buy/<uuid> buy-link
LS_TEAM_CHECKOUT_URL=
FREE_TIER_MONTHLY_LIMIT=100    # optional; runs/month before upgrade required

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

## Migration #2

Per-user API keys for the CI/CD auth path. Only the sha256 **hash** is stored;
the raw `sqv_…` key is shown once at creation. Server lookups use the service
key (RLS-bypassing) so `resolve_api_key()` can match a hash across all users.

```sql
CREATE TABLE IF NOT EXISTS public.api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_prefix   TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_user_id_idx  ON public.api_keys (user_id);
CREATE INDEX IF NOT EXISTS api_keys_key_hash_idx ON public.api_keys (key_hash);

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_select_own_keys"
    ON public.api_keys FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "users_insert_own_keys"
    ON public.api_keys FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_update_own_keys"
    ON public.api_keys FOR UPDATE TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE ON public.api_keys TO authenticated;
```

`SUPABASE_ANON_KEY` (already in the env list) is now used by `POST /auth/magic-link`
to call Supabase's OTP endpoint.

## Migration #3

Link Lemon Squeezy subscriptions to a SQLVerify user so plan/quota resolve by
`user_id`. The in-app checkout (`/billing/checkout`) passes `user_id` as LS
checkout custom data; the webhook stores it.

```sql
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS user_id UUID
        REFERENCES auth.users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS subscriptions_user_id_idx
    ON public.subscriptions (user_id);
```

## Migration #4

Per-user **projects** to group verification runs. Mirrors the api_keys pattern:
RLS enabled with per-user policies for defense-in-depth, while the service key
bypasses RLS and the repos scope by `owner_id` in code. `verification_runs`
gains a nullable `project_id` (FK `ON DELETE SET NULL`, so deleting a project
keeps its runs but unlinks them). The verify endpoints validate `project_id`
ownership (`_resolve_project_id`) before tagging a run. Full SQL lives in
`supabase/migrations/20260620000001_add_project.sql` — `projects` table
(`owner_id`, `name`, `description`, `UNIQUE (owner_id, name)`) + 4 RLS policies +
GRANT, then `ALTER TABLE verification_runs ADD COLUMN project_id` + index.

## Billing (Lemon Squeezy — Merchant of Record)

LS handles global sales tax/VAT; we never touch card data. Flow:

- **Checkout** — `GET /billing/checkout?plan=individual|team` (`api/billing.py`)
  redirects to the plan's LS buy-link with `checkout[custom][user_id]` so the
  webhook can link the sub to the buyer. Pricing-page CTAs point here.
- **Webhook** — `POST /api/webhooks/lemonsqueezy` (signature-verified) upserts
  `subscriptions` on `subscription_created/updated/cancelled/expired/resumed/
  payment_failed`, reading `meta.custom_data.user_id`.
- **Portal** — `GET /billing/portal` fetches a fresh signed customer-portal URL
  from the LS API and redirects.
- **Free tier** — `FREE_TIER_MONTHLY_LIMIT` (default 100) verification runs per
  UTC calendar month, counted by `user_id`; any active paid tier
  (`individual`/`team`) lifts the cap. Enforced in `_enforce_quota()` on both
  `POST /api/verify` (HTMX 402 → `partials/upgrade_prompt.html`) and
  `POST /api/verify/text` (JSON 402). **Fails open** on a lookup error.
