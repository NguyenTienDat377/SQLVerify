# Skolem

**Formal verification for AI-generated SQL — catch semantic bugs before they reach production.**

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com/)

---

## What is Skolem?

LLMs generate SQL that _looks_ correct. Skolem proves whether it _is_ correct.

Given a Flyway DDL schema and two SQL queries (e.g. an original and an AI-rewritten version), it uses **Z3 SMT solving** to either:

- **Prove the two queries are semantically equivalent** — not just syntactically similar, but guaranteed to return the same results on any valid database within the search bound
- **Find the exact counterexample that breaks them** — a concrete counterexample database showing precisely where and why they diverge

This is formal, deterministic verification. Not probabilistic checking. Not linting. If Skolem says two queries are equivalent, they are (within the documented bound and SQL subset).

```
You paste:   AI-generated SQL + your DDL schema
Skolem:   Either proves equivalence ✅
             Or shows you the exact input rows that expose the bug ❌
```

---

## Why it exists

AI coding tools now generate SQL in production pipelines. The problem: LLMs are probabilistic — they produce plausible-looking output, not guaranteed-correct output. SQL bugs are binary — a wrong JOIN either loses rows or duplicates them. There's no "mostly correct."

Skolem is the missing guardrail between AI-generated SQL and your production database.

---

## Three delivery surfaces, one engine

The same Z3 core powers three ways to use Skolem:

- **Web tool** — backend engineers reviewing AI-generated SQL before it ships, via the HTMX UI (`POST /api/verify`) with an on-demand **Explain** button. Snappy timeout (default 15s).
- **CI/CD tool** — automated SQL validation in pipelines and AI-agent loops, via the JSON endpoint (`POST /api/verify/text`), authenticated with a per-user API key and always explaining divergent results. Verdict-favouring timeout (default 60s).
- **MCP tool** — AI coding agents (Claude Code/Desktop, Cursor, …) call verification **in-loop** via a thin stdio proxy ([`mcp/`](mcp/)) that forwards to the JSON endpoint. This powers a counterexample-driven **self-healing loop**: an agent proposes a rewrite, gets a proof or a concrete counterexample, and revises against ground truth until proven equivalent ([`mcp/examples/repair_loop.py`](mcp/examples/repair_loop.py)).

---

## Quick Start

```bash
# Clone the repo
git clone https://github.com/yourusername/skolem.git
cd skolem

# Set up environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Fill in the keys below

# Run locally
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`.

---

## Environment Variables

```env
ANTHROPIC_API_KEY=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_KEY=
EXPLAINER_PROVIDER=claude        # claude | openai | google
SITE_URL=                        # full origin, e.g. https://skolem.dev (no trailing slash) — auth redirects + CORS

# Billing (Lemon Squeezy — Merchant of Record)
LEMONSQUEEZY_WEBHOOK_SECRET=     # HMAC signing secret (Settings → Webhooks)
LEMONSQUEEZY_API_KEY=            # for the customer portal lookup
LS_INDIVIDUAL_VARIANT_ID=        # numeric variant id (webhook → tier map)
LS_TEAM_VARIANT_ID=
LS_INDIVIDUAL_CHECKOUT_URL=      # the .../checkout/buy/<uuid> buy-link
LS_TEAM_CHECKOUT_URL=
FREE_TIER_MONTHLY_LIMIT=100      # optional; runs/month before upgrade required
```

Config is read via `os.getenv` per-module — there is no `config.py`. All of these go into Render's environment variable panel; never commit them.

---

## Architecture

Skolem is a **modular monolith** — clear separation of concerns, no microservices complexity.

```
skolem/
│
├── main.py                  # FastAPI entry: routers, pinned CORS, rate-limit + JWT middleware,
│                            #   error handlers (404/500), public pages (/, /pricing, /terms,
│                            #   /privacy, /robots.txt, /sitemap.xml)
│
├── core/                    # Z3 engine — pure Python, no FastAPI, no DB
│   ├── models.py            # Dataclasses: Column, ForeignKey, Table, SchemaModel, VerificationResult
│   ├── ddl_parser.py        # Flyway DDL → SchemaModel via sqlglot
│   ├── sql_encoder.py       # SELECT query + SchemaModel → Z3 QueryFormula
│   ├── equivalence.py       # Two formulas → VerificationResult + counterexample (+ SQLite cross-check)
│   ├── constraint_check.py  # ⬜ stub — single-query constraint checking (Direction 2)
│   └── witness.py           # ⬜ stub — counterexample formatting (handled inline today)
│
├── explainer/               # LLM provider abstraction — swap Claude/GPT/Gemini via env var
│   ├── explain.py           # explain_result() public API (guarded by the circuit breaker)
│   ├── providers.py         # LLMProvider ABC + Anthropic/OpenAI/Google + get_provider() factory
│   ├── circuit_breaker.py   # async breaker — fail fast when the LLM provider is down
│   └── prompts.py           # equivalence + constraint explanation prompt templates
│
├── api/                     # FastAPI routers
│   ├── verify.py            # POST /api/verify, POST /api/verify/text, POST /api/explain/{run_id},
│   │                        #   GET /api/history[/{run_id}], GET /api/verify/health
│   ├── auth.py              # GitHub OAuth + magic-link + session cookies + logout
│   ├── keys.py              # per-user API key CRUD (HTMX partials)
│   ├── projects.py          # per-user project CRUD (HTMX partials)
│   ├── billing.py           # Lemon Squeezy checkout + customer portal redirects
│   └── webhooks.py          # Lemon Squeezy subscription webhooks (HMAC-verified)
│
├── auth/                    # JWTMiddleware: Supabase JWT (browser) OR per-user API key (CI)
│   └── middleware.py
│
├── db/                      # Supabase client + repositories (service key, scoped by user_id in code)
│   ├── client.py
│   └── repositories/        # verification_runs, subscriptions, api_keys, projects
│
├── mcp/                     # MCP surface — standalone stdio proxy (own .venv; only needs mcp+httpx)
│   ├── skolem_mcp.py     # FastMCP server: tool verify_sql_equivalence() → POST /api/verify/text
│   └── examples/            # repair_loop.py — counterexample-driven self-healing agent loop
│
└── web/                     # Jinja2 + HTMX frontend
    ├── templates/           # base, landing, verify, pricing, keys, projects, terms, privacy, 404, 500
    │   └── partials/        # result, history, api_keys, projects, upgrade_prompt
    └── static/              # css + img (favicon, hero, og-image)
```

### Key Design Decisions

| Decision                                      | Rationale                                                                                                                                                                                           |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Z3 SMT solver for verification                | Deterministic, formal — not heuristic. Counterexamples are mathematically guaranteed within the bound.                                                                                              |
| `bound=3` hardcoded                           | Catches >95% of real SQL semantic bugs. Not exposed in the UI — would confuse engineers. Power users can override via env var.                                                                      |
| Fail-closed parsing/encoding                  | Any SQL outside the supported subset raises an error instead of being silently dropped. A dropped predicate can produce a false "equivalent" — the one failure mode a verifier must not have.       |
| Witness cross-check                           | On a `divergent` verdict, both queries run on the SQLite witness; if outputs agree, the verdict is downgraded to `error` (encoder bug) rather than showing a fake counterexample.                   |
| HTMX for frontend                             | No React build step, no npm. Server-rendered, fast, simple.                                                                                                                                         |
| Multi-LLM provider abstraction                | Adding a new LLM = one new class in `providers.py` + one line in `get_provider()`. Never lock into one vendor.                                                                                      |
| Explainer is on-demand only                   | The "Explain" button is an explicit user action. Never auto-call the LLM on every verification — cost and latency.                                                                                  |
| Circuit breaker on LLM calls                  | Fails fast when the provider is down so an outage doesn't burn credits/latency.                                                                                                                     |
| Per-IP rate limiting (slowapi)                | The two solve endpoints are throttled (default 30/min) — verification is expensive, so an unthrottled endpoint lets one client pin a worker.                                                        |
| Per-surface Z3 timeouts                       | Web defaults 15s (cap 60s) for a snappy UI; CI defaults 60s (clamp 120s) to favour a verdict. On timeout the result is `unknown` — never a wrong answer.                                            |
| Two auth paths, one `user_id`                 | Session JWT (browser) or per-user API key (CI), both resolved by `JWTMiddleware`. Access control is enforced in app code (ownership checks + repo scoping), not RLS — the service key bypasses RLS. |
| CORS pinned, no wildcard                      | Allowlist of `SITE_URL` + localhost, `GET`/`POST`, credentialed CORS off.                                                                                                                           |
| CSRF via `SameSite=Lax` + POST-only mutations | No CSRF token by design — Lax cookies block cross-site POSTs, all mutations are POST, and the CI/API path uses header auth (no ambient credentials).                                                |
| Render for deployment (not serverless)        | Z3 binary is too heavy for Lambda cold starts. Always-on container is the right fit.                                                                                                                |
| Supabase for DB + auth                        | Managed PostgreSQL + built-in auth. Service key stays server-side only.                                                                                                                             |
| Billing via Lemon Squeezy (MoR)               | Merchant of Record handles global VAT; we never touch card data. Free tier fails **open** so a metering hiccup never blocks a user.                                                                 |

---

## How It Works

### 1. Parse the Schema (DDL → SchemaModel)

```sql
-- Input: your Flyway migration DDL
CREATE TABLE accounts (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    balance REAL NOT NULL
);
-- Output: SchemaModel with tables, columns, types, FK/PK/NOT NULL constraints
```

### 2. Encode Queries as Z3 Formulas

Each query becomes a symbolic formula over a bounded database (`bound=3` rows per table). NULLs are modeled as a `(is_null, value)` pair under three-valued logic. The encoder handles `SELECT`, `WHERE`/`HAVING`, INNER/LEFT/RIGHT/FULL `JOIN`, `GROUP BY`, and aggregates.

### 3. Check Equivalence (bag semantics)

Z3 tries to find a database instance where the two formulas produce different results:

- **UNSAT** → no such instance exists → queries are **equivalent** ✅
- **SAT** → here are the exact rows that expose the difference → queries **diverge** ❌ (then verified by re-running both on the concrete SQLite witness)

### 4. Explain (Optional)

On user request — or always, for the CI endpoint — the counterexample is passed to your configured LLM provider for a plain-English explanation of why the queries diverge. The call is wrapped in a circuit breaker, so an LLM outage degrades gracefully instead of breaking verification.

---

## Supported SQL Subset & Limitations

Everything outside the supported subset is **rejected with a clear error** (fail-closed), never silently ignored. The subset is deliberately wide and widening over time.

- **Joins** — any left-deep chain mixing INNER/LEFT/RIGHT/FULL joins, each with a single column-equality `ON`. A LEFT/FULL step null-extends the table it joins; a RIGHT/FULL step null-extends the whole accumulated left side (every table joined so far), matching the paper's own binary-join semantics generalized to a chain. GROUP BY may key on any table in the chain, including a null-extended side. No CROSS or self-joins.
- **NULLs are modeled** — three-valued logic per the [VeriEQL paper](docs/references/veriEQL-2024.pdf): `IS [NOT] NULL`, `COUNT(col)` vs `COUNT(*)`, and LEFT/RIGHT/FULL JOIN null-extension are all encoded. NOT NULL / PK columns are forced non-NULL; FK columns may be NULL.
- **Predicates** — AND-chains of comparisons and `IS [NOT] NULL` only. No `OR` / `IN` / `BETWEEN` / `LIKE`. Column-vs-literal and column-vs-column both supported. Boolean literals (`active = TRUE`) rejected.
- **No** CTEs (`WITH`), window functions, subqueries, `UNION`, `SELECT DISTINCT`, `SELECT *`, `LIMIT`/`OFFSET`.
- **Text/Timestamp equality** is symbolic (interned to integer) — ordering comparisons (`>`, `<`) on text/timestamp are rejected.
- **`GROUP BY`** merges equal-key rows (Dedup); a bare SELECT column must appear in GROUP BY. `HAVING` supports aggregate comparisons (the aggregate need not be in the SELECT list).
- **Bag semantics** via tuple multiplicity — `ORDER BY` is accepted but ignored (no list semantics).
- **CHECK constraints** are parsed but not encoded into Z3 (FK/PK/NOT NULL cover most real bugs).
- **`bound=3`** — may miss bugs requiring more row interactions (rare in practice). Deep INNER chains (n≥4) get slower and may return `unknown` on timeout — the bound is never silently lowered.
- **No cross-dialect comparison** — both queries must target the same SQL dialect.

---

## Supabase Schema

```sql
CREATE TABLE verification_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status              TEXT NOT NULL,     -- equivalent | divergent | unknown | error
    divergence_reason   TEXT,
    counterexample_db   JSONB,
    query_v1_output     JSONB,
    query_v2_output     JSONB,
    error_message       TEXT,
    explanation         TEXT,              -- filled in by /api/explain
    ddl_input           TEXT,
    query_a_input       TEXT,
    query_b_input       TEXT,
    result_json         JSONB,
    bound               INT DEFAULT 3,
    duration_ms         INT,
    created_at          TIMESTAMPTZ DEFAULT now()
);
```

Plus migrations adding `user_id` (Migration #1), the `api_keys` table (#2), `subscriptions.user_id` (#3), and the `projects` table with `verification_runs.project_id` (#4). RLS is enabled on every table; the service key in `db/client.py` intentionally bypasses RLS for server-side writes, and the repos scope by `user_id`/`owner_id` in code. See [CLAUDE.md](CLAUDE.md) for the full migration SQL.

---

## Billing

Lemon Squeezy as Merchant of Record handles global sales tax/VAT — we never touch card data.

- **Checkout** — `GET /billing/checkout?plan=individual|team` redirects to the plan's LS buy-link, passing `user_id` as checkout custom data.
- **Webhook** — `POST /api/webhooks/lemonsqueezy` (HMAC-verified) upserts subscriptions on create/update/cancel/expire/resume/payment-failed.
- **Portal** — `GET /billing/portal` fetches a fresh signed customer-portal URL and redirects.
- **Free tier** — `FREE_TIER_MONTHLY_LIMIT` (default 100) runs per UTC calendar month, counted by `user_id`; any active paid tier lifts the cap. Enforced on both solve endpoints and **fails open** on a lookup error.

---

## Tests

All suites are standalone (pytest-compatible but no pytest required): `.venv/bin/python tests/<file>.py`.

- `tests/smoke_test.py` — 32 end-to-end checks over `check_equivalence()`.
- `tests/paper_cases_test.py` — 21 regression tests from the VeriEQL paper.
- `tests/differential_test.py` — differential fuzzing: random query pairs verified by Z3, cross-checked against concrete SQLite (`--seeds N --bound B`).
- `tests/api_verify_text_test.py` — `/api/verify/text` end-to-end (timeouts, persistence, explain) with Supabase/LLM stubbed.
- `tests/api_keys_test.py` — key hashing + dual auth path + magic-link OTP.
- `tests/circuit_breaker_test.py` — the explainer circuit breaker.
- `tests/billing_test.py` — free-tier quota gate, checkout, portal, webhook.

---

## Roadmap

**Current** — Equivalence checking via web UI, CI/CD JSON endpoint, **and** an MCP tool for in-loop AI agents, with GitHub/magic-link auth, per-user API keys & projects, and Lemon Squeezy billing.

**Next** — Widen the supported SQL subset (see the SQL-subset-expansion roadmap), single-query constraint checking (Direction 2), and business-intent checks against a user-supplied expectation.

**Scale** — Async job queue + competing consumers (Postgres `SKIP LOCKED`), result cache, shared-state rate-limiter/breaker, poison-job watchdog (see the scaling roadmap), PostHog.

**AI Agent Guardrails** — for self-healing AI agents. Foundations shipped: the MCP surface ([`mcp/`](mcp/)) and a counterexample-driven repair-loop demo ([`mcp/examples/repair_loop.py`](mcp/examples/repair_loop.py)). Next: a published `skolem-mcp` package and a hosted (remote) MCP server so agents connect without a local install.

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

This means: if you run Skolem as a network service, you must make your modifications open source under the same license. Commercial license available for teams that need different terms.

---

## Contributing

Issues and PRs are welcome. If you're fixing a bug in the Z3 encoding layer (`core/`), please include a test case — a known-divergent query pair that previously passed as equivalent.

## Acknowledgments

Skolem's verification engine is built on academic research — see [NOTICE.md](NOTICE.md) for citations and licensing.
