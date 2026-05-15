# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env

# Run the API server (auto-reload)
uvicorn main:app --reload --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/core/test_equivalence.py

# Run a single test by name
pytest tests/core/test_equivalence.py::test_name -v
```

The Swagger UI is available at `http://localhost:8000/docs` when the server is running.

## Required Environment Variables

Configured via `.env` (see `.env.example`). Loaded by `config.py` using Pydantic BaseSettings:

- `SUPABASE_URL` / `SUPABASE_KEY` — Supabase database connection
- `ANTHROPIC_API_KEY` — For LLM explanation generation
- `SECRET_KEY` — Optional, defaults to `"change-me"`

## Architecture

SQLVerify is a **formal SQL equivalence checker**: given a schema (DDL) and two SELECT queries, it uses the Z3 SMT solver to determine whether the queries always return the same results, or it finds a minimal counterexample database that proves they diverge.

### Verification Pipeline (Direction 1 — implemented)

```
DDL text ──▶ parse_ddl() ──▶ SchemaModel
                                  │
               sql_v1, sql_v2 ──▶ parse_query() ──▶ ParsedQuery × 2
                                  │
                             SymbolicDB (Z3 variables, one set shared by both queries)
                                  │
                             encode_query() ──▶ QueryFormula × 2
                                  │
                             Z3 Solver: assert divergence, check SAT
                                  │
                  SAT ──▶ _materialize_witness() ──▶ SQLite in-memory DB
                                  │
                          execute both queries ──▶ VerificationResult
```

Key modules:
- [core/models.py](core/models.py) — Pure data models: `Column`, `Table`, `SchemaModel`, `ForeignKey`, `VerificationResult`
- [core/ddl_parser.py](core/ddl_parser.py) — Parses `CREATE TABLE` DDL via sqlglot into `SchemaModel`. Normalizes all SQL types to `INTEGER | REAL | TEXT | BOOLEAN | TIMESTAMP`.
- [core/sql_encoder.py](core/sql_encoder.py) — Parses SELECT queries into `ParsedQuery`, builds `SymbolicDB` (Z3 variables for each table/column/row slot, bounded by `DEFAULT_BOUND = 3`), encodes queries into Z3 `QueryFormula`.
- [core/equivalence.py](core/equivalence.py) — Orchestrates the full pipeline via `check_equivalence()`. On SAT, materializes the Z3 model into a concrete SQLite DB, runs both queries, and returns their actual outputs.
- [api/verify.py](api/verify.py) — FastAPI router with two endpoints: `POST /api/verify` (file upload) and `POST /api/verify/text` (JSON). Limits: `MAX_BOUND = 6`, `MAX_FILE_BYTES = 512 KB`, `timeout_ms` capped at 60 s.
- [explainer/prompts.py](explainer/prompts.py) — Prompt templates for calling Claude to explain equivalence divergences or constraint violations in plain English.

### Direction 2 — Constraint Checking (stub only)

[core/constraint_check.py](core/constraint_check.py) and [core/witness.py](core/witness.py) are empty stubs for a planned feature that would verify whether a single query satisfies a stated property.

### Z3 Encoding Details

- **Bound**: Each table gets `bound` symbolic rows (default 3). Higher = more coverage but exponentially slower.
- **Text/Timestamp columns** are encoded as `Int` via string interning (literals mapped to small integers).
- **Domain constraints** enforce PK uniqueness, FK integrity, NOT NULL, and value ranges.
- **Divergence assertion**: The solver checks `∃ db. query_v1(db) ≠ query_v2(db)`. SAT means divergent; UNSAT means equivalent within the bound; timeout → `"unknown"`.

### SQL Subset Supported by the Parser (V1)

- `SELECT`: column refs, `SUM`, `COUNT(*)`, `COUNT(col)`, `COALESCE(SUM(...), 0)`
- `FROM`: single table with alias
- `JOIN`: one `INNER` or `LEFT JOIN` with simple equality `ON`
- `WHERE`: `AND` chains of `=`, `>`, `>=`, `<`, `<=`, `IS NULL`, `IS NOT NULL`
- `GROUP BY`: one or more columns; `HAVING`: simple aggregate comparisons

Not supported: window functions, CTEs, subqueries, `UNION`, `ORDER BY`, `LIMIT`.

### DDL Parser Limitations

Handles `CREATE TABLE` only. Does not handle `ALTER TABLE`, `CREATE INDEX`, or `CREATE VIEW`. Intended for Flyway-style migration files.
