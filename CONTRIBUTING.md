# Contributing to SQLVerify

Thanks for your interest in contributing. SQLVerify is a formal verification tool for AI-generated SQL — the core engine is Z3/SMT based, so contributions here require some care to not break solver correctness. This document explains how to contribute safely.

---

## Before You Start

**Read this first:** SQLVerify's value proposition is *deterministic correctness*. A bug in the Z3 encoding layer (`core/`) is worse than a missing feature — it means the tool silently passes incorrect SQL. Every change to `core/` must come with a test case.

---

## What We Welcome

### Good first issues
- Improving error messages when Z3 times out or hits an unsupported query shape
- Adding more example queries to the demo UI
- Documentation fixes and README improvements
- UI/UX improvements to `web/templates/`
- New LLM provider in `explainer/providers.py` (see guide below)

### Bigger contributions
- Expanding SQL coverage in `core/sql_encoder.py` (CTEs, subqueries, window functions)
- Improving NULL modeling in Z3 encoding
- GitHub Action wrapper for CI/CD integration
- Test suite expansion

### What we are NOT looking for right now
- Switching the core solver away from Z3
- Rewriting the frontend from HTMX to React
- Changing the modular monolith structure to microservices

If you're unsure, open an issue first and ask before writing code.

---

## Setup

```bash
git clone https://github.com/NguyenTienDat377/sqlverify.git
cd sqlverify

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Fill in your keys — see README for what's needed

uvicorn main:app --reload --port 8000
```

---

## Project Structure (Quick Reference)

```
core/          Z3 engine — DDL parsing, SQL encoding, equivalence checking
explainer/     LLM provider abstraction (Claude / OpenAI / Google)
api/           FastAPI routers
db/            Supabase client
auth/          Auth middleware
web/           Jinja2 + HTMX frontend
```

Full details in the README.

---

## The Rule for core/ Changes

**Every bug fix or feature in `core/` must include a test case.**

A test case is a pair of SQL queries that are either:
- **Known equivalent** — Z3 should return `EQUIVALENT`
- **Known divergent** — Z3 should return `DIVERGENT` with a specific counterexample

This is non-negotiable. The reason: Z3 encoding bugs are silent. A wrong formula doesn't crash — it just returns the wrong answer. Without a test case, there's no way to verify the fix is correct or catch regressions later.

Example test case format:

```python
# tests/test_equivalence.py

def test_inner_join_where_order_equivalent():
    """WHERE before vs after JOIN should be equivalent for INNER JOIN."""
    ddl = """
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL);
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
    """
    query_a = "SELECT u.name, o.amount FROM orders o JOIN users u ON o.user_id = u.id WHERE o.amount > 100"
    query_b = "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id WHERE o.amount > 100"

    result = verify(ddl, query_a, query_b)
    assert result.status == "equivalent"


def test_left_join_null_filter_divergent():
    """LEFT JOIN with IS NOT NULL filter diverges from INNER JOIN semantics."""
    ddl = """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance REAL);
        CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), amount REAL);
    """
    query_a = "SELECT a.id FROM accounts a LEFT JOIN transactions t ON a.id = t.account_id"
    query_b = "SELECT a.id FROM accounts a LEFT JOIN transactions t ON a.id = t.account_id WHERE t.id IS NOT NULL"

    result = verify(ddl, query_a, query_b)
    assert result.status == "divergent"
```

---

## Adding a New LLM Provider

The `explainer/` layer is designed for exactly this. Steps:

1. Open `explainer/providers.py`
2. Add a new class that implements the `LLMProvider` interface:

```python
class YourProvider(LLMProvider):
    def explain(self, prompt: str) -> str:
        # Your API call here
        ...
```

3. Register it in `get_provider()`:

```python
def get_provider() -> LLMProvider:
    provider = os.getenv("EXPLAINER_PROVIDER", "claude")
    if provider == "yourprovider":
        return YourProvider()
    ...
```

4. Add `yourprovider` to the `EXPLAINER_PROVIDER` options in `.env.example` and the README.

That's it. No changes needed anywhere else in the codebase.

---

## Pull Request Guidelines

- **One concern per PR.** Don't mix a bug fix in `core/` with a UI change — they're reviewed differently.
- **`core/` changes require tests** — see above. PRs without tests will be asked to add them before merge.
- **Keep the V1 scope in mind.** If your PR adds CTE support, it's welcome but needs a discussion first — scope expansion touches multiple files.
- **Don't break the existing test cases.** Run the test suite before opening a PR.

```bash
pytest tests/
```

---

## Reporting Bugs

If you found a query pair where SQLVerify gives the wrong answer (says equivalent when it's not, or vice versa), that's the most valuable bug report you can file.

Please include:
- The DDL
- Both query versions
- What SQLVerify returned
- What you expected

Open an issue with the label `wrong-result`.

---

## License

By contributing, you agree that your contributions are licensed under the same [GNU Affero General Public License v3.0](LICENSE) as the rest of the project.
