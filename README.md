# SQLVerify

**Formal verification for AI-generated SQL — catch semantic bugs before they reach production.**

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com/)

---

## What is SQLVerify?

LLMs generate SQL that _looks_ correct. SQLVerify proves whether it _is_ correct.

It uses **Z3 SMT solving** to either:

- **Prove two SQL queries are semantically equivalent** — not just syntactically similar, but guaranteed to return the same results on any valid database
- **Find the exact counterexample that breaks them** — a concrete set of rows showing precisely where and why two queries diverge

This is formal, deterministic verification. Not probabilistic checking. Not linting. If SQLVerify says two queries are equivalent, they are.

```
You paste:   AI-generated SQL + your DDL schema
SQLVerify:   Either proves equivalence ✅
             Or shows you the exact input rows that expose the bug ❌
```

---

## Why it exists

AI coding tools now generate SQL in production pipelines. The problem: LLMs are probabilistic — they produce plausible-looking output, not guaranteed-correct output. SQL bugs are binary — a wrong JOIN either loses rows or duplicates them. There's no "mostly correct."

SQLVerify is the missing guardrail between AI-generated SQL and your production database.

---

## Quick Start

```bash
# Clone the repo
git clone https://github.com/yourusername/sqlverify.git
cd sqlverify

# Set up environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY

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
```

---

## Architecture

SQLVerify is a **modular monolith** — clear separation of concerns, no microservices complexity.

```
sqlverify/
│
├── main.py                  # FastAPI entry point, mounts all routers
├── config.py                # Environment variable loading
│
├── core/                    # Z3 engine — pure Python, no FastAPI, no DB
│   ├── models.py            # Dataclasses: SchemaModel, QueryFormula, VerificationResult
│   ├── ddl_parser.py        # DDL SQL → SchemaModel via sqlglot
│   ├── sql_encoder.py       # SELECT query → Z3 formula
│   ├── equivalence.py       # Two formulas → VerificationResult + counterexample
│   ├── constraint_check.py  # Single query property checking (Direction 2)
│   └── witness.py           # Z3 model → human-readable counterexample table
│
├── explainer/               # LLM provider abstraction — swap Claude/GPT/Gemini freely
│   ├── client.py            # Provider factory
│   ├── providers.py         # Claude, OpenAI, Google implementations
│   └── prompts.py           # Prompt templates for counterexample explanation
│
├── api/                     # FastAPI routers
│   ├── verify.py            # POST /api/verify (file upload + JSON body)
│   └── explain.py           # POST /api/explain (on-demand LLM explanation)
│
├── db/                      # Supabase client + query helpers
│   └── client.py
│
├── auth/                    # Supabase auth middleware
│   └── middleware.py
│
└── web/                     # Jinja2 + HTMX frontend
    ├── router.py
    ├── templates/
    │   ├── base.html
    │   ├── verify.html
    │   └── partials/
    │       ├── result.html
    │       └── history.html
    └── static/
```

### Key Design Decisions

| Decision                               | Rationale                                                                                                           |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Z3 SMT solver for verification         | Deterministic, formal — not heuristic. Counterexamples are mathematically guaranteed.                               |
| `bound=3` hardcoded                    | Catches >95% of real SQL semantic bugs. Exposing it would confuse most users. Power users can override via env var. |
| HTMX for frontend                      | No React build step, no npm. Server-rendered, fast, simple.                                                         |
| Multi-LLM provider abstraction         | Adding a new LLM = one new class in `providers.py`. Never lock into one vendor.                                     |
| Explainer is on-demand only            | The "Explain" button is an explicit user action. Never auto-call LLM on every verification — cost and latency.      |
| Render for deployment (not serverless) | Z3 binary is ~50MB. Cold starts on Lambda are too slow. Always-on container is the right fit.                       |
| Supabase for DB + auth                 | Managed PostgreSQL + built-in auth. No self-hosted infra. Service key stays server-side only.                       |

---

## How It Works

### 1. Parse the Schema (DDL → SchemaModel)

```python
# Input: your Flyway migration DDL
CREATE TABLE accounts (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    balance REAL NOT NULL
);

# Output: SchemaModel with tables, columns, types, FK constraints
```

### 2. Encode Queries as Z3 Formulas

Each query becomes a symbolic formula over a bounded database (`bound=3` rows per table). The encoder handles `SELECT`, `WHERE`, `JOIN` (INNER and LEFT), `GROUP BY`, and aggregates.

### 3. Check Equivalence

Z3 tries to find a database instance where the two formulas produce different results. Two outcomes:

- **UNSAT** → no such instance exists → queries are **equivalent** ✅
- **SAT** → here are the exact rows that expose the difference → queries **diverge** ❌

### 4. Explain (Optional)

On user request, the counterexample is passed to your configured LLM provider for a plain-English explanation of why the queries diverge.

---

## V1 Scope and Limitations

SQLVerify V1 is intentionally constrained. These are known limitations, not bugs:

- **Single JOIN per query only** — INNER JOIN and LEFT JOIN supported. No RIGHT, FULL OUTER.
- **No CTEs** — `WITH` clauses are not supported.
- **No window functions** — `ROW_NUMBER()`, `RANK()`, etc. are out of scope.
- **No subqueries or UNION**
- **NULLs** — not modeled as a distinct domain value in Z3. Three-valued SQL logic is a V2 item.
- **Text/Timestamp equality** is symbolic (interned to integer) — string ordering not supported.
- **CHECK constraints** — parsed but not encoded into Z3. FK/PK covers the majority of real bugs.
- **`bound=3`** — may miss bugs requiring 4+ row interactions. Rare in practice.
- **No cross-dialect comparison** — both queries must be written for the same SQL dialect.

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

RLS is enabled. The service key in `db/client.py` bypasses RLS for server-side writes only.

---

## Roadmap

**V1 (current)** — Single JOIN equivalence checking via web UI

**V2** — CI/CD integration as a GitHub Action, CTE and subquery support, AI agent pipeline mode

**V3** — Multi-dialect comparison, expanded Z3 encoding (full NULL modeling, window functions), RabbitMQ + worker pool for scale

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

This means: if you run SQLVerify as a network service, you must make your modifications open source under the same license. Commercial license available for teams that need different terms.

---

## Contributing

Issues and PRs are welcome. If you're fixing a bug in the Z3 encoding layer (`core/`), please include a test case — a known-divergent query pair that previously passed as equivalent.

## Acknowledgments

SQLVerify's verification engine is built on academic research — see [NOTICE.md](NOTICE.md) for citations and licensing.
