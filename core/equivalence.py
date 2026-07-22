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
from collections import Counter
from typing import Optional

import z3
from z3 import (
    And, Bool, BoolVal, If, Implies, Int, IntSort, IntVal, Not, Or,
    Real, RealVal, Solver, Sum, ToReal, is_true, sat, unknown, unsat,
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

    # 9. Soundness guardrail: the witness must actually reproduce the
    # divergence. If SQLite returns identical bags for both queries, the Z3
    # encoding disagreed with real SQL semantics — report the bug instead of
    # showing the user a fake counterexample.
    if _witness_outputs_differ(v1_output, v2_output) is False:
        return VerificationResult(
            status="error",
            error_message=(
                "Internal inconsistency: the solver reported a divergence, but "
                "both queries return identical results on the generated witness "
                "database. This indicates an encoding bug in SQLVerify — do not "
                "trust this verdict; please report it with the schema and queries."
            ),
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

def _val_eq(x, y):
    """Sort-safe equality of two Z3 arithmetic values (coerce Int↔Real)."""
    try:
        return x == y
    except Exception:
        xr = ToReal(x) if x.sort() == IntSort() else x
        yr = ToReal(y) if y.sort() == IntSort() else y
        return xr == yr


def _col_eq(a, b):
    """NULL-aware column equality for bag-multiplicity comparison.
    Following the paper (§4.5): two NULLs are considered equal, and two
    non-NULL values are equal iff their underlying values match."""
    return Or(
        And(a.is_null, b.is_null),
        And(Not(a.is_null), Not(b.is_null), _val_eq(a.value, b.value)),
    )


def _tuple_eq(t, r) -> object:
    """Two output tuples are equal iff all corresponding columns are equal."""
    if len(t.cols) != len(r.cols):
        return BoolVal(False)
    return And([_col_eq(t.cols[k], r.cols[k]) for k in range(len(t.cols))])


def _assert_diverges(
    f1: QueryFormula,
    f2: QueryFormula,
    bound: int,
) -> object:
    """
    Build the Z3 assertion that the two queries produce different output bags on
    the same symbolic database — the negation of the paper's bag-equivalence
    formula (Eqns 1–2):

      (1) the two outputs have the same number of present (non-deleted) tuples, and
      (2) every present tuple in R1 has the same multiplicity in R1 and in R2.

    Two queries with different output arity are trivially non-equivalent.
    """
    if f1.arity != f2.arity:
        return BoolVal(True)

    r1, r2 = f1.output, f2.output

    # (1) equal cardinality of present tuples
    count1 = Sum([If(t.present, IntVal(1), IntVal(0)) for t in r1]) if r1 else IntVal(0)
    count2 = Sum([If(t.present, IntVal(1), IntVal(0)) for t in r2]) if r2 else IntVal(0)
    same_count = count1 == count2

    # (2) per-tuple multiplicity agreement (quantified over R1; combined with
    # (1) this also rules out any extra distinct tuple in R2)
    mult_ok = []
    for t in r1:
        m1 = Sum([If(And(u.present, _tuple_eq(t, u)), IntVal(1), IntVal(0)) for u in r1])
        m2 = (Sum([If(And(u.present, _tuple_eq(t, u)), IntVal(1), IntVal(0)) for u in r2])
              if r2 else IntVal(0))
        mult_ok.append(Implies(t.present, m1 == m2))

    bag_equivalent = And(same_count, And(mult_ok)) if mult_ok else same_count
    return Not(bag_equivalent)


# ---------------------------------------------------------------------------
# Divergence classification
# ---------------------------------------------------------------------------

def _eval_tuples(model, formula: QueryFormula) -> list:
    """Materialise a query's present output tuples from the model as a list of
    value lists, where None denotes NULL."""
    rows = []
    for t in formula.output:
        if not is_true(model.evaluate(t.present, model_completion=True)):
            continue
        row = []
        for sv in t.cols:
            if is_true(model.evaluate(sv.is_null, model_completion=True)):
                row.append(None)
            else:
                row.append(_z3_to_python(model.evaluate(sv.value, model_completion=True)))
        rows.append(row)
    return rows


def _classify_divergence(
    model,
    f1: QueryFormula,
    f2: QueryFormula,
    bound: int,
) -> str:
    """Inspect the Z3 model to produce a human-readable divergence reason."""
    if f1.arity != f2.arity:
        return (
            f"output shape differs: v1 returns {f1.arity} column(s), "
            f"v2 returns {f2.arity} column(s)"
        )

    rows1 = _eval_tuples(model, f1)
    rows2 = _eval_tuples(model, f2)

    parts = []
    if len(rows1) != len(rows2):
        parts.append(
            f"row count differs: v1 returns {len(rows1)} row(s), "
            f"v2 returns {len(rows2)} row(s)"
        )
    else:
        # Same cardinality but the bags differ in content/multiplicity.
        b1 = Counter(tuple(r) for r in rows1)
        b2 = Counter(tuple(r) for r in rows2)
        if b1 != b2:
            parts.append("row values differ for the same number of rows")

    return "; ".join(parts) if parts else "queries produce different output"


def _witness_outputs_differ(
    v1_output: list[dict],
    v2_output: list[dict],
) -> Optional[bool]:
    """Compare the two SQLite outputs as bags of value tuples.

    Returns True when they differ, False when they are identical, and None
    when either query failed to execute (so no conclusion can be drawn —
    e.g. dialect-specific syntax SQLite cannot run).
    """
    def bag(rows: list[dict]) -> Optional[Counter]:
        out = []
        for r in rows:
            if len(r) == 1 and "error" in r:
                return None   # execution failed; can't compare
            out.append(tuple(r.values()))
        return Counter(out)

    b1, b2 = bag(v1_output), bag(v2_output)
    if b1 is None or b2 is None:
        return None
    return b1 != b2


# ---------------------------------------------------------------------------
# Witness materialization
# ---------------------------------------------------------------------------

def _z3_to_python(z3_val) -> object:
    """Convert a Z3 model value to a Python int/float/str.

    Note: a Real (RatNumRef) model value *has* an ``as_long`` method, but it
    raises "Expected integer fraction" for non-integer rationals (e.g. -1/2).
    We therefore branch on the value *kind* rather than attribute presence.
    """
    if z3_val is None:
        return None
    try:
        if z3.is_int_value(z3_val):
            return z3_val.as_long()
        if z3.is_rational_value(z3_val):
            return float(z3_val.as_fraction())
        if z3.is_algebraic_value(z3_val):
            return float(z3_val.as_decimal(10).rstrip("?"))
    except Exception:
        pass
    # Fallbacks for anything unexpected.
    try:
        return z3_val.as_long()
    except Exception:
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

    # Reverse the string-interning map so TEXT/TIMESTAMP cells are written back
    # as real strings. Without this, a cell holding the interned int for
    # 'active' is materialised as the integer 1, and the real query
    # `WHERE status = 'active'` no longer matches — so the witness fails to
    # reproduce the divergence under SQLite. The intern namespace is global
    # (one code per distinct string), so equal codes decode to equal strings
    # across columns and column-to-column TEXT comparisons stay consistent.
    inv_intern: dict[int, str] = {code: lit for lit, code in db._str_map.items()}

    def decode(table_name, col_name, value):
        col = db.schema.get_table(table_name) and db.schema.get_table(table_name).get_column(col_name)
        if col is None or col.col_type not in ("TEXT", "TIMESTAMP"):
            return value
        if not isinstance(value, int):
            return value
        if value in inv_intern:
            return inv_intern[value]
        # A value never compared against a literal: synthesise a string that is
        # guaranteed distinct from every interned literal.
        return f"__sv_{value}"

    for table_name, col_vars in db.vars.items():
        # Skip materialised CTE relations: they are derived from base tables, not
        # inputs to the witness DB. SQLite recomputes each CTE from the base rows
        # when the original query (which still contains its WITH clause) replays.
        if table_name in db.cte_col_types:
            continue
        rows = []
        for i in range(db.bound):
            exists_var = db.exists[table_name][i]
            if not is_true(model.evaluate(exists_var, model_completion=True)):
                continue
            row = {}
            for col_name, var_list in col_vars.items():
                null_var = db.nulls[table_name][col_name][i]
                if is_true(model.evaluate(null_var, model_completion=True)):
                    row[col_name] = None   # NULL cell
                else:
                    raw = _z3_to_python(
                        model.evaluate(var_list[i], model_completion=True)
                    )
                    row[col_name] = decode(table_name, col_name, raw)
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
            # Safe: row values are parameterized (see `values` below); only
            # identifiers (table/column names) are interpolated, and those come
            # from a sqlglot-parsed SchemaModel, not raw user text. SQLite can't
            # bind identifiers, and this targets a throwaway :memory: DB.
            cur.execute(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",  # nosec B608
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