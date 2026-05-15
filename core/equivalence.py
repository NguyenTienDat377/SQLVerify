"""
core/equivalence.py

Main public API for Direction 1: migration/refactor verification.

Given two SQL queries (v1 = original, v2 = AI-generated) and a DDL schema,
find a concrete minimal database where the two queries return different results.

Usage:
    result = check_equivalence(
        ddl_sql   = open('V3__schema.sql').read(),
        sql_v1    = open('query_original.sql').read(),
        sql_v2    = open('query_ai_rewrite.sql').read(),
        dialect   = 'postgres',
        bound     = 3,
        timeout_ms= 10_000,
    )

    if result.status == 'divergent':
        print(result.divergence_reason)
        print(result.counterexample_db)
        print(result.query_v1_output)
        print(result.query_v2_output)
    elif result.status == 'equivalent':
        print('Safe within verification bounds.')
    elif result.status == 'unknown':
        print('Solver timed out - try a smaller bound.')
"""

import sqlite3
from typing import Optional

from z3 import (
    And, Bool, BoolVal, If, Implies, Int, IntVal, Not, Or,
    Real, RealVal, Solver, is_true, sat, unknown, unsat,
)

from core.ddl_parser import parse_ddl
from core.models import SchemaModel, VerificationResult
from core.sql_encoder import (
    DEFAULT_BOUND,
    ParsedQuery,
    QueryFormula,
    SymbolicDB,
    build_symbolic_db,
    encode_query,
    parse_query,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_equivalence(
    ddl_sql: str,
    sql_v1: str,
    sql_v2: str,
    dialect: str = "generic",
    bound: int = DEFAULT_BOUND,
    timeout_ms: int = 15_000,
) -> VerificationResult:
    """
    Check whether sql_v1 and sql_v2 are semantically equivalent under ddl_sql.

    Args:
        ddl_sql:    CREATE TABLE DDL (Flyway file content).
        sql_v1:     Original trusted query.
        sql_v2:     AI-generated / rewritten query to verify.
        dialect:    SQL dialect: 'postgres' | 'mysql' | 'sqlite' | 'snowflake' etc.
        bound:      Symbolic rows per table. Higher = more coverage, slower.
                    Default 3 catches all JOIN/aggregation divergences.
        timeout_ms: Z3 solver timeout in milliseconds. Returns 'unknown' on timeout.

    Returns:
        VerificationResult with status, counterexample DB, and both query outputs.
    """
    try:
        return _run(ddl_sql, sql_v1, sql_v2, dialect, bound, timeout_ms)
    except ValueError as e:
        return VerificationResult(status="error", error_message=str(e))
    except Exception as e:
        return VerificationResult(
            status="error",
            error_message=f"Unexpected error during verification: {e}",
        )


# ---------------------------------------------------------------------------
# Internal pipeline
# ---------------------------------------------------------------------------

def _run(
    ddl_sql: str,
    sql_v1: str,
    sql_v2: str,
    dialect: str,
    bound: int,
    timeout_ms: int,
) -> VerificationResult:

    # 1. Parse DDL -> SchemaModel
    schema = parse_ddl(ddl_sql, dialect=dialect)

    # 2. Parse both queries
    parsed_v1 = parse_query(sql_v1, dialect=dialect)
    parsed_v2 = parse_query(sql_v2, dialect=dialect)

    # 3. Build shared symbolic DB (same Z3 variables for both formulas)
    db = build_symbolic_db(schema, bound=bound)

    # 4. Encode both queries as Z3 formulas over the same symbolic DB
    formula_v1 = encode_query(parsed_v1, db, schema)
    formula_v2 = encode_query(parsed_v2, db, schema)

    # 5. Build and run the solver
    solver = Solver()
    solver.set("timeout", timeout_ms)

    # Domain constraints from schema (PK, FK, NOT NULL, bounds)
    solver.add(db.domain_constraints())

    # Query-specific constraints (HAVING refinements)
    solver.add(formula_v1.extra_constraints)
    solver.add(formula_v2.extra_constraints)

    # Divergence assertion: find a DB where the two queries differ
    solver.add(_assert_diverges(formula_v1, formula_v2, db.bound))

    result = solver.check()

    if result == unsat:
        return VerificationResult(status="equivalent")

    if result == unknown:
        return VerificationResult(
            status="unknown",
            error_message=(
                f"Solver timed out after {timeout_ms}ms with bound={bound}. "
                "Try reducing the bound or increasing the timeout."
            ),
        )

    # SAT: counterexample found
    model = solver.model()

    # 6. Determine what kind of divergence was found
    reason = _classify_divergence(model, formula_v1, formula_v2, db.bound)

    # 7. Materialize Z3 model into a concrete SQLite database
    counterexample_db = _materialize_witness(model, db)

    # 8. Run both original SQL queries against the witness DB to show the diff
    v1_output, v2_output = _run_queries_on_witness(
        counterexample_db, schema, sql_v1, sql_v2
    )

    return VerificationResult(
        status="divergent",
        counterexample_db=counterexample_db,
        query_v1_output=v1_output,
        query_v2_output=v2_output,
        divergence_reason=reason,
    )


# ---------------------------------------------------------------------------
# Divergence assertion
# ---------------------------------------------------------------------------

def _assert_diverges(
    f1: QueryFormula,
    f2: QueryFormula,
    bound: int,
) -> object:
    """
    Build the Z3 assertion that the two query formulas produce different output
    on the same symbolic database.

    Two queries diverge if for any group i:
      - One includes it in output and the other doesn't (row presence differs), OR
      - Both include it but with different aggregate values (value differs)
    """
    presence_differs = [
        f1.in_output[i] != f2.in_output[i]
        for i in range(bound)
    ]

    value_differs = []
    # Find aggregate aliases that appear in both formulas
    common_agg_aliases = set(f1.agg_values.keys()) & set(f2.agg_values.keys())
    # Exclude bare column aliases (not aggregates) — these can't differ independently
    agg_only = {
        alias for alias in common_agg_aliases
        if alias not in (f1.group_key_vars or {})
    }

    for alias in agg_only:
        for i in range(bound):
            value_differs.append(
                And(
                    f1.in_output[i],
                    f2.in_output[i],
                    f1.agg_values[alias][i] != f2.agg_values[alias][i],
                )
            )

    all_divergences = presence_differs + value_differs
    if not all_divergences:
        # Fallback: just assert presence differs (no aggregates in query)
        return Or([f1.in_output[i] != f2.in_output[i] for i in range(bound)])

    return Or(all_divergences)


# ---------------------------------------------------------------------------
# Divergence classification
# ---------------------------------------------------------------------------

def _classify_divergence(
    model,
    f1: QueryFormula,
    f2: QueryFormula,
    bound: int,
) -> str:
    """
    Inspect the Z3 model to produce a human-readable divergence reason.
    Returns one of:
      - "row presence differs: v1 returns N rows, v2 returns M rows"
      - "aggregate value differs for matching rows"
      - "both row presence and aggregate value differ"
    """
    presence_diff_rows = []
    value_diff_rows = []

    for i in range(bound):
        in_v1 = is_true(model.evaluate(f1.in_output[i]))
        in_v2 = is_true(model.evaluate(f2.in_output[i]))

        if in_v1 != in_v2:
            presence_diff_rows.append(i)
        elif in_v1 and in_v2:
            for alias in set(f1.agg_values.keys()) & set(f2.agg_values.keys()):
                v1_val = model.evaluate(f1.agg_values[alias][i])
                v2_val = model.evaluate(f2.agg_values[alias][i])
                if str(v1_val) != str(v2_val):
                    value_diff_rows.append(i)
                    break

    parts = []
    if presence_diff_rows:
        v1_count = sum(1 for i in range(bound) if is_true(model.evaluate(f1.in_output[i])))
        v2_count = sum(1 for i in range(bound) if is_true(model.evaluate(f2.in_output[i])))
        parts.append(
            f"row presence differs: v1 returns {v1_count} row(s), v2 returns {v2_count} row(s)"
        )
    if value_diff_rows:
        parts.append("aggregate value differs for row(s) present in both outputs")

    return "; ".join(parts) if parts else "queries produce different output"


# ---------------------------------------------------------------------------
# Witness materialization
# ---------------------------------------------------------------------------

def _z3_to_python(z3_val) -> object:
    """Convert a Z3 model value to a Python int/float/str."""
    if z3_val is None:
        return None
    if hasattr(z3_val, "as_long"):
        return z3_val.as_long()
    if hasattr(z3_val, "as_fraction"):
        num, den = z3_val.as_fraction()
        return float(num) / float(den)
    if hasattr(z3_val, "as_decimal"):
        try:
            return float(z3_val.as_decimal(6).rstrip("?"))
        except Exception:
            pass
    return str(z3_val)


def _materialize_witness(model, db: SymbolicDB) -> dict:
    """
    Convert a Z3 satisfying model into a concrete dict of table rows.

    Returns:
        {
          'accounts': [{'account_id': 1, 'name': 1, 'status': 1}, ...],
          'transactions': [],
        }
    """
    result: dict[str, list[dict]] = {}

    for table_name, col_vars in db.vars.items():
        rows = []
        for i in range(db.bound):
            exists_var = db.exists[table_name][i]
            if not is_true(model[exists_var]):
                continue
            row = {}
            for col_name, var_list in col_vars.items():
                row[col_name] = _z3_to_python(model[var_list[i]])
            rows.append(row)
        result[table_name] = rows

    return result


# ---------------------------------------------------------------------------
# Query execution on witness DB
# ---------------------------------------------------------------------------

def _build_sqlite_db(counterexample_db: dict, schema: SchemaModel) -> sqlite3.Connection:
    """Create an in-memory SQLite DB and insert the witness rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Create tables in schema order (respect FK dependencies)
    for table_name, table in schema.tables.items():
        col_defs = []
        for col in table.columns:
            # Map normalized type back to SQLite type
            sqlite_type = {
                "INTEGER": "INTEGER",
                "REAL": "REAL",
                "TEXT": "TEXT",
                "BOOLEAN": "INTEGER",
                "TIMESTAMP": "TEXT",
            }.get(col.col_type, "TEXT")
            col_defs.append(f"{col.name} {sqlite_type}")
        cur.execute(f"CREATE TABLE {table_name} ({', '.join(col_defs)})")

    # Insert witness rows
    for table_name, rows in counterexample_db.items():
        if not rows:
            continue
        col_names = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in col_names)
        col_list = ", ".join(col_names)
        for row in rows:
            values = [row[c] for c in col_names]
            cur.execute(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",
                values,
            )

    conn.commit()
    return conn


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Convert sqlite3.Row results to plain dicts."""
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _run_queries_on_witness(
    counterexample_db: dict,
    schema: SchemaModel,
    sql_v1: str,
    sql_v2: str,
) -> tuple[list[dict], list[dict]]:
    """
    Run both original SQL queries against the materialized witness DB.
    Returns (v1_output_rows, v2_output_rows).
    """
    conn = _build_sqlite_db(counterexample_db, schema)
    cur = conn.cursor()

    try:
        cur.execute(sql_v1)
        v1_output = _rows_to_dicts(cur)
    except sqlite3.Error as e:
        v1_output = [{"error": str(e)}]

    try:
        cur.execute(sql_v2)
        v2_output = _rows_to_dicts(cur)
    except sqlite3.Error as e:
        v2_output = [{"error": str(e)}]

    conn.close()
    return v1_output, v2_output