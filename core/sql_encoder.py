"""
core/sql_encoder.py

Parses a SELECT query and encodes it as Z3 formulas over a symbolic database.

V1 supported subset:
  SELECT   — column refs, SUM, COUNT(*), COUNT(col), COALESCE(SUM(...), 0)
  FROM     — single table with alias
  JOIN     — one INNER JOIN or LEFT JOIN with simple ON equality condition
  WHERE    — AND chains of EQ / GT / GTE / LT / LTE / IS NULL / IS NOT NULL
  GROUP BY — one or more columns from the FROM table
  HAVING   — simple aggregate comparisons (SUM > val, COUNT > val)

Out of scope for V1:
  Window functions, CTEs, subqueries, UNION, ORDER BY, LIMIT

Usage:
    db     = build_symbolic_db(schema, bound=3)
    parsed = parse_query(sql, dialect='postgres')
    formula = encode_query(parsed, db, schema)

    # In equivalence.py:
    solver.add(db.domain_constraints())
    solver.add(formula_a.extra_constraints)
    solver.add(formula_b.extra_constraints)
    solver.add(assert_diverges(formula_a, formula_b))
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import sqlglot
import sqlglot.expressions as exp
from z3 import (
    And, Bool, BoolVal, If, Implies, Int, IntVal, Or, Real, RealVal,
    Sum, Not, ArithRef, BoolRef, Z3Exception
)

from core.models import SchemaModel


# ── Bound ────────────────────────────────────────────────────────────────────

DEFAULT_BOUND = 5  # symbolic rows per table
                    # increase for more coverage, decrease for speed


# ── Parsed query structures ──────────────────────────────────────────────────

@dataclass
class ParsedCondition:
    """A single WHERE / HAVING predicate."""
    op: str                          # 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte'
                                     # | 'is_null' | 'is_not_null'
                                     # | 'having_gt' | 'having_gte' | 'having_lt' | 'having_lte'
    table_alias: Optional[str]       # alias of the table (e.g. 'a', 't')
    col: Optional[str]               # column name
    value: Optional[object]          # literal value (int/float/str) or None
    agg_type: Optional[str] = None   # 'sum' | 'count_star' | 'count_col' for HAVING
    agg_col: Optional[str] = None    # column inside aggregate for HAVING
    agg_table_alias: Optional[str] = None


@dataclass
class ParsedSelectExpr:
    """One expression in the SELECT list."""
    alias: str                    # output column name
    expr_type: str                # 'column' | 'sum' | 'count_star' | 'count_col'
    table_alias: Optional[str]    # source table alias
    col_name: Optional[str]       # source column name (None for COUNT(*))


@dataclass
class ParsedJoin:
    table_name: str
    alias: str
    join_type: str                # 'INNER' | 'LEFT'
    on_left_alias: str            # alias on left side of ON condition
    on_left_col: str
    on_right_alias: str           # alias on right side of ON condition
    on_right_col: str


@dataclass
class ParsedQuery:
    from_table: str
    from_alias: str
    joins: list[ParsedJoin]
    select_exprs: list[ParsedSelectExpr]
    group_by: list[tuple[str, str]]      # [(table_alias, col_name)]
    where_conditions: list[ParsedCondition]
    having_conditions: list[ParsedCondition]


# ── Symbolic database ────────────────────────────────────────────────────────

class SymbolicDB:
    """
    A bounded symbolic database: N rows per table, each column is a Z3 variable.

    TEXT and TIMESTAMP columns are encoded as Int (symbolic enum).
    REAL columns are encoded as Real.
    INTEGER / BOOLEAN columns are encoded as Int.

    String literals in WHERE clauses are interned to integers via intern_string().
    """

    def __init__(self, schema: SchemaModel, bound: int = DEFAULT_BOUND):
        self.schema = schema
        self.bound = bound
        # vars[table][col][row_idx] → Z3 variable
        self.vars: dict[str, dict[str, list]] = {}
        # exists[table][row_idx] → Bool
        self.exists: dict[str, list[BoolRef]] = {}
        # string interning: (table_name, col_name, literal) → int
        self._str_map: dict[tuple, int] = {}
        self._str_counter: dict[tuple[str, str], int] = defaultdict(lambda: 1)
        self._build()

    def _build(self) -> None:
        for table_name, table in self.schema.tables.items():
            self.exists[table_name] = [
                Bool(f"{table_name}_exists_{i}")
                for i in range(self.bound)
            ]
            self.vars[table_name] = {}
            for col in table.columns:
                if col.col_type == "REAL":
                    self.vars[table_name][col.name] = [
                        Real(f"{table_name}_{col.name}_{i}")
                        for i in range(self.bound)
                    ]
                else:
                    # INTEGER, TEXT, BOOLEAN, TIMESTAMP → all Int in Z3
                    self.vars[table_name][col.name] = [
                        Int(f"{table_name}_{col.name}_{i}")
                        for i in range(self.bound)
                    ]

    def get_var(self, table_name: str, col_name: str, row_idx: int):
        """Retrieve the Z3 variable for table.column[row_idx]."""
        if table_name not in self.vars:
            raise KeyError(f"Table '{table_name}' not in symbolic DB.")
        if col_name not in self.vars[table_name]:
            raise KeyError(f"Column '{col_name}' not in table '{table_name}'.")
        return self.vars[table_name][col_name][row_idx]

    def intern_string(self, table_name: str, col_name: str, value: str) -> int:
        """
        Map a string literal to a stable integer for Z3 equality checks.
        'active' → 1, 'inactive' → 2, etc. (per column namespace)
        """
        key = (table_name, col_name, value)
        if key not in self._str_map:
            counter_key = (table_name, col_name)
            self._str_map[key] = self._str_counter[counter_key]
            self._str_counter[counter_key] += 1
        return self._str_map[key]

    def domain_constraints(self) -> list:
        """
        Generate all Z3 constraints derived from the SchemaModel:
          - Column value bounds
          - Primary key uniqueness
          - Foreign key referential integrity
          - NOT NULL (implied by bounds when nullable=False)
        """
        constraints = []

        for table_name, table in self.schema.tables.items():
            exists = self.exists[table_name]

            for i in range(self.bound):
                row_exists = exists[i]

                for col in table.columns:
                    var = self.vars[table_name][col.name][i]

                    if col.col_type == "INTEGER":
                        constraints.append(
                            Implies(row_exists, And(var >= 1, var <= self.bound * 4))
                        )
                    elif col.col_type == "REAL":
                        constraints.append(
                            Implies(row_exists, And(var >= RealVal(0), var <= RealVal(self.bound * 1000)))
                        )
                    elif col.col_type in ("TEXT", "TIMESTAMP", "BOOLEAN"):
                        # Symbolic enum: small integer domain
                        constraints.append(
                            Implies(row_exists, And(var >= 1, var <= self.bound * 3))
                        )

            # ── Primary key uniqueness ───────────────────────────────────────
            for pk_col in table.primary_key_columns:
                if pk_col not in self.vars[table_name]:
                    continue
                pk_vars = self.vars[table_name][pk_col]
                for i in range(self.bound):
                    for j in range(i + 1, self.bound):
                        constraints.append(
                            Implies(
                                And(exists[i], exists[j]),
                                pk_vars[i] != pk_vars[j],
                            )
                        )

            # ── Foreign key referential integrity ────────────────────────────
            for fk in table.foreign_keys:
                ref_tname = fk.references_table
                if ref_tname not in self.vars:
                    continue  # referenced table not in schema, skip

                src_col_name = fk.columns[0] if fk.columns else None
                # Resolve reference column: explicit or PK of target
                ref_col_name = (
                    fk.references_columns[0]
                    if fk.references_columns
                    else (
                        self.schema.get_table(ref_tname).primary_key_columns[0]
                        if self.schema.get_table(ref_tname)
                        and self.schema.get_table(ref_tname).primary_key_columns
                        else None
                    )
                )

                if not src_col_name or not ref_col_name:
                    continue
                if src_col_name not in self.vars[table_name]:
                    continue
                if ref_col_name not in self.vars[ref_tname]:
                    continue

                src_vars = self.vars[table_name][src_col_name]
                ref_vars = self.vars[ref_tname][ref_col_name]
                ref_exists = self.exists[ref_tname]

                for i in range(self.bound):
                    constraints.append(
                        Implies(
                            exists[i],
                            Or([
                                And(ref_exists[j], src_vars[i] == ref_vars[j])
                                for j in range(self.bound)
                            ]),
                        )
                    )

        return constraints


# ── Query formula ────────────────────────────────────────────────────────────

@dataclass
class QueryFormula:
    """
    Z3 encoding of a SELECT query's output, indexed by FROM-table row.

    For a query grouping by account_id:
      in_output[i]            = Bool: does account i appear in the result?
      agg_values['total'][i]  = ArithRef: aggregated value for account i
      group_key_vars['account_id'][i] = Z3 variable: the GROUP BY key value

    extra_constraints contains no assertions about divergence —
    those live in equivalence.py.
    """
    in_output: list[BoolRef]
    agg_values: dict[str, list]          # alias → list of Z3 expressions (per group)
    group_key_vars: dict[str, list]      # col_name → list of Z3 vars (per group)
    extra_constraints: list
    bound: int


# ── SQL parser ───────────────────────────────────────────────────────────────

def _parse_conditions(node: exp.Expression) -> list[ParsedCondition]:
    """Recursively extract WHERE / HAVING conditions from an AST node."""
    if node is None:
        return []

    results: list[ParsedCondition] = []

    if isinstance(node, exp.And):
        results.extend(_parse_conditions(node.left))
        results.extend(_parse_conditions(node.right))
        return results

    # EQ: col = literal  or  col = col
    if isinstance(node, exp.EQ):
        left, right = node.this, node.expression
        if isinstance(left, exp.Column):
            tbl = left.args.get("table")
            val = _literal_value(right)
            results.append(ParsedCondition(
                op="eq",
                table_alias=tbl.name if tbl else None,
                col=left.name,
                value=val,
            ))
        return results

    # Comparison operators
    op_map = {
        exp.GT: "gt", exp.GTE: "gte",
        exp.LT: "lt", exp.LTE: "lte",
        exp.NEQ: "neq",
    }
    for node_type, op_str in op_map.items():
        if isinstance(node, node_type):
            left, right = node.this, node.expression
            # Column comparison
            if isinstance(left, exp.Column):
                tbl = left.args.get("table")
                results.append(ParsedCondition(
                    op=op_str,
                    table_alias=tbl.name if tbl else None,
                    col=left.name,
                    value=_literal_value(right),
                ))
                return results
            # Aggregate in HAVING (SUM/COUNT > val)
            if isinstance(left, (exp.Sum, exp.Count)):
                agg_type, agg_col, agg_tbl = _parse_aggregate(left)
                results.append(ParsedCondition(
                    op=f"having_{op_str}",
                    table_alias=None,
                    col=None,
                    value=_literal_value(right),
                    agg_type=agg_type,
                    agg_col=agg_col,
                    agg_table_alias=agg_tbl,
                ))
                return results

    # IS NULL / IS NOT NULL
    if isinstance(node, exp.Is):
        col_node = node.this
        if isinstance(col_node, exp.Column):
            tbl = col_node.args.get("table")
            is_null = isinstance(node.expression, exp.Null)
            results.append(ParsedCondition(
                op="is_null" if is_null else "is_not_null",
                table_alias=tbl.name if tbl else None,
                col=col_node.name,
                value=None,
            ))

    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Is):
            col_node = inner.this
            if isinstance(col_node, exp.Column):
                tbl = col_node.args.get("table")
                results.append(ParsedCondition(
                    op="is_not_null",
                    table_alias=tbl.name if tbl else None,
                    col=col_node.name,
                    value=None,
                ))
        else:
            negation_map = {
                "gt": "lte", "gte": "lt",
                "lt": "gte", "lte": "gt",
                "eq": "neq", "neq": "eq",
            }
            for cond in _parse_conditions(inner):
                if cond.op in negation_map:
                    results.append(ParsedCondition(
                        op=negation_map[cond.op],
                        table_alias=cond.table_alias,
                        col=cond.col,
                        value=cond.value,
                        agg_type=cond.agg_type,
                        agg_col=cond.agg_col,
                        agg_table_alias=cond.agg_table_alias,
                    ))

    return results


def _literal_value(node: exp.Expression) -> Optional[object]:
    """Extract a Python int/float/str from a literal AST node."""
    if isinstance(node, exp.Literal):
        if node.is_number:
            val = node.this
            return float(val) if "." in val else int(val)
        return node.this   # string (without quotes)
    if isinstance(node, exp.Null):
        return None
    return None


def _parse_aggregate(node: exp.Expression) -> tuple[str, Optional[str], Optional[str]]:
    """
    Return (agg_type, col_name, table_alias) from a Sum or Count AST node.
    agg_type: 'sum' | 'count_star' | 'count_col'
    """
    if isinstance(node, exp.Sum):
        col = node.find(exp.Column)
        tbl = col.args.get("table") if col else None
        return "sum", (col.name if col else None), (tbl.name if tbl else None)

    if isinstance(node, exp.Count):
        arg = node.this
        if isinstance(arg, exp.Star):
            return "count_star", None, None
        col = node.find(exp.Column)
        tbl = col.args.get("table") if col else None
        return "count_col", (col.name if col else None), (tbl.name if tbl else None)

    return "unknown", None, None


def _parse_select_expr(node: exp.Expression) -> Optional[ParsedSelectExpr]:
    """Extract a ParsedSelectExpr from one SELECT list item."""
    # Bare column: SELECT a.account_id
    if isinstance(node, exp.Column):
        tbl = node.args.get("table")
        return ParsedSelectExpr(
            alias=node.name,
            expr_type="column",
            table_alias=tbl.name if tbl else None,
            col_name=node.name,
        )

    # Aliased expression: SELECT SUM(t.amount) AS total_spend
    if isinstance(node, exp.Alias):
        alias_name = node.alias
        inner = node.this

        # COALESCE(SUM(...), 0) — treat as SUM for V1
        if isinstance(inner, exp.Coalesce):
            inner = inner.this  # unwrap to SUM inside

        if isinstance(inner, exp.Sum):
            agg_type, col_name, tbl_alias = _parse_aggregate(inner)
            return ParsedSelectExpr(
                alias=alias_name,
                expr_type="sum",
                table_alias=tbl_alias,
                col_name=col_name,
            )

        if isinstance(inner, exp.Count):
            agg_type, col_name, tbl_alias = _parse_aggregate(inner)
            return ParsedSelectExpr(
                alias=alias_name,
                expr_type=agg_type,   # 'count_star' or 'count_col'
                table_alias=tbl_alias,
                col_name=col_name,
            )

        if isinstance(inner, exp.Column):
            tbl = inner.args.get("table")
            return ParsedSelectExpr(
                alias=alias_name,
                expr_type="column",
                table_alias=tbl.name if tbl else None,
                col_name=inner.name,
            )

    return None  # unsupported expression — skipped


def parse_query(sql: str, dialect: str = "generic") -> ParsedQuery:
    """
    Parse a SELECT SQL string into a structured ParsedQuery.

    Args:
        sql:     Raw SELECT query string.
        dialect: SQL dialect for parsing.

    Returns:
        ParsedQuery with all fields populated.

    Raises:
        ValueError: If SQL is not a SELECT or uses unsupported constructs.
    """
    if not sql or not sql.strip():
        raise ValueError("Query SQL is empty.")

    sqlglot_dialect = None if dialect == "generic" else dialect
    stmt = sqlglot.parse_one(sql, dialect=sqlglot_dialect)

    if not isinstance(stmt, exp.Select):
        raise ValueError("Only SELECT queries are supported.")

    # ── FROM table ───────────────────────────────────────────────────────────
    from_clause = stmt.find(exp.From)
    if from_clause is None:
        raise ValueError("Query has no FROM clause.")

    from_table_node = from_clause.find(exp.Table)
    if from_table_node is None:
        raise ValueError("Could not identify FROM table.")

    from_table = from_table_node.name
    from_alias = from_table_node.alias or from_table

    # ── JOINs (V1: max one join) ─────────────────────────────────────────────
    joins: list[ParsedJoin] = []
    for join_node in stmt.find_all(exp.Join):
        tbl = join_node.find(exp.Table)
        if tbl is None:
            continue

        kind = join_node.args.get("kind")   # 'INNER' string or None
        side = join_node.args.get("side")   # 'LEFT' string or None

        if side == "LEFT":
            join_type = "LEFT"
        elif side == "RIGHT":
            raise ValueError("RIGHT JOIN is not supported in V1. Rewrite as LEFT JOIN.")
        else:
            join_type = "INNER"   # default: INNER

        on = join_node.args.get("on")
        if on is None or not isinstance(on, exp.EQ):
            raise ValueError(
                f"JOIN ON clause must be a simple equality condition. Got: {on}"
            )

        left_col, right_col = on.this, on.expression
        if not isinstance(left_col, exp.Column) or not isinstance(right_col, exp.Column):
            raise ValueError("JOIN ON must compare two columns directly.")

        left_tbl = left_col.args.get("table")
        right_tbl = right_col.args.get("table")

        joins.append(ParsedJoin(
            table_name=tbl.name,
            alias=tbl.alias or tbl.name,
            join_type=join_type,
            on_left_alias=left_tbl.name if left_tbl else from_alias,
            on_left_col=left_col.name,
            on_right_alias=right_tbl.name if right_tbl else (tbl.alias or tbl.name),
            on_right_col=right_col.name,
        ))

    # ── SELECT expressions ───────────────────────────────────────────────────
    select_exprs: list[ParsedSelectExpr] = []
    for sel in stmt.expressions:
        parsed_sel = _parse_select_expr(sel)
        if parsed_sel is not None:
            select_exprs.append(parsed_sel)

    if not select_exprs:
        raise ValueError("No supported SELECT expressions found.")

    # ── GROUP BY ─────────────────────────────────────────────────────────────
    group_by: list[tuple[str, str]] = []
    group_node = stmt.args.get("group")
    if group_node:
        for col in group_node.find_all(exp.Column):
            tbl = col.args.get("table")
            group_by.append((tbl.name if tbl else from_alias, col.name))

    # ── WHERE ────────────────────────────────────────────────────────────────
    where_conditions: list[ParsedCondition] = []
    where_node = stmt.args.get("where")
    if where_node:
        where_conditions = _parse_conditions(where_node.this)

    # ── HAVING ───────────────────────────────────────────────────────────────
    having_conditions: list[ParsedCondition] = []
    having_node = stmt.args.get("having")
    if having_node:
        having_conditions = _parse_conditions(having_node.this)

    return ParsedQuery(
        from_table=from_table,
        from_alias=from_alias,
        joins=joins,
        select_exprs=select_exprs,
        group_by=group_by,
        where_conditions=where_conditions,
        having_conditions=having_conditions,
    )


# ── Z3 encoder ───────────────────────────────────────────────────────────────

def build_symbolic_db(schema: SchemaModel, bound: int = DEFAULT_BOUND) -> SymbolicDB:
    """
    Build a SymbolicDB from a SchemaModel.
    Call this ONCE and pass to both encode_query() calls in equivalence.py.
    """
    return SymbolicDB(schema=schema, bound=bound)


def encode_query(
    parsed: ParsedQuery,
    db: SymbolicDB,
    schema: SchemaModel,
) -> QueryFormula:
    """
    Encode a ParsedQuery into a QueryFormula using the symbolic database.

    Args:
        parsed:  Output of parse_query().
        db:      Shared SymbolicDB — must be the SAME instance for both queries
                 being compared in equivalence.py.
        schema:  The SchemaModel (used for alias → table name resolution).

    Returns:
        QueryFormula with in_output, agg_values, group_key_vars, extra_constraints.
    """
    bound = db.bound

    # ── Alias → real table name resolution ───────────────────────────────────
    alias_map: dict[str, str] = {parsed.from_alias: parsed.from_table}
    for join in parsed.joins:
        alias_map[join.alias] = join.table_name

    def resolve(alias: str) -> str:
        """Resolve a query alias to the real table name in the schema."""
        return alias_map.get(alias, alias)

    from_tname = resolve(parsed.from_alias)

    # ── Z3 helpers ───────────────────────────────────────────────────────────
    def var(alias: str, col: str, i: int):
        return db.get_var(resolve(alias), col, i)

    def row_exists(alias: str, i: int) -> BoolRef:
        return db.exists[resolve(alias)][i]

    # ── Build WHERE filter for a given pair of rows (left=i, right=j) ────────
    def where_filter_left(i: int) -> BoolRef:
        """WHERE conditions that depend only on the left (FROM) table row i."""
        clauses = []
        for cond in parsed.where_conditions:
            if cond.table_alias not in (parsed.from_alias, None):
                continue
            clause = _condition_to_z3(cond, alias_map, db, i, None, schema)
            if clause is not None:
                clauses.append(clause)
        return And(clauses) if clauses else BoolVal(True)

    def where_filter_right(i: int, j: int) -> BoolRef:
        """WHERE conditions on the right (JOIN) table row j, in context of row i."""
        if not parsed.joins:
            return BoolVal(True)
        join_alias = parsed.joins[0].alias
        clauses = []
        for cond in parsed.where_conditions:
            if cond.table_alias != join_alias:
                continue
            clause = _condition_to_z3(cond, alias_map, db, i, j, schema)
            if clause is not None:
                clauses.append(clause)
        return And(clauses) if clauses else BoolVal(True)

    # ── JOIN match predicate ──────────────────────────────────────────────────
    # match(i, j): left row i joins to right row j
    def join_match(i: int, j: int) -> BoolRef:
        if not parsed.joins:
            return BoolVal(False)
        join = parsed.joins[0]
        join_tname = resolve(join.alias)

        left_tname = resolve(join.on_left_alias)
        right_tname = resolve(join.on_right_alias)

        left_var = db.get_var(left_tname, join.on_left_col, i)
        right_var = db.get_var(right_tname, join.on_right_col, j)

        return And(
            row_exists(join.alias, j),
            left_var == right_var,
            where_filter_right(i, j),
        )

    def has_match(i: int) -> BoolRef:
        """True if left row i has at least one matching right row."""
        if not parsed.joins:
            return BoolVal(True)
        return Or([join_match(i, j) for j in range(bound)])

    # ── in_output: which left rows appear in the query result ────────────────
    in_output: list[BoolRef] = []
    for i in range(bound):
        base = And(row_exists(parsed.from_alias, i), where_filter_left(i))
        if not parsed.joins:
            in_output.append(base)
        else:
            join = parsed.joins[0]
            if join.join_type == "LEFT":
                in_output.append(base)
            else:  # INNER
                in_output.append(And(base, has_match(i)))

    # ── Aggregate values per output group ────────────────────────────────────
    agg_values: dict[str, list] = {}

    for sel in parsed.select_exprs:
        if sel.expr_type == "column":
            # Direct column — just the variable value
            vals = []
            for i in range(bound):
                v = db.get_var(resolve(sel.table_alias or parsed.from_alias), sel.col_name, i)
                vals.append(v)
            agg_values[sel.alias] = vals

        elif sel.expr_type == "sum":
            # SUM(t.amount) per group i
            join_alias = sel.table_alias
            col_name = sel.col_name
            sums = []
            for i in range(bound):
                if not parsed.joins:
                    sums.append(IntVal(0))
                    continue
                s = Sum([
                    If(join_match(i, j), db.get_var(resolve(join_alias), col_name, j), IntVal(0))
                    for j in range(bound)
                ])
                sums.append(s)
            agg_values[sel.alias] = sums

        elif sel.expr_type == "count_star":
            # COUNT(*) — count matching rows
            counts = []
            for i in range(bound):
                if not parsed.joins:
                    counts.append(If(in_output[i], IntVal(1), IntVal(0)))
                    continue
                c = Sum([
                    If(join_match(i, j), IntVal(1), IntVal(0))
                    for j in range(bound)
                ])
                counts.append(c)
            agg_values[sel.alias] = counts

        elif sel.expr_type == "count_col":
            # COUNT(t.col) — counts non-NULL matching rows
            # In our encoding NULLs aren't modeled explicitly, treat same as count_star
            join_alias = sel.table_alias
            col_name = sel.col_name
            counts = []
            for i in range(bound):
                if not parsed.joins:
                    counts.append(IntVal(0))
                    continue
                c = Sum([
                    If(join_match(i, j), IntVal(1), IntVal(0))
                    for j in range(bound)
                ])
                counts.append(c)
            agg_values[sel.alias] = counts

    # ── GROUP BY key variables ────────────────────────────────────────────────
    group_key_vars: dict[str, list] = {}
    for (tbl_alias, col_name) in parsed.group_by:
        tname = resolve(tbl_alias)
        if col_name in db.vars.get(tname, {}):
            group_key_vars[col_name] = db.vars[tname][col_name]

    # ── HAVING filter — additional in_output refinement ──────────────────────
    extra_constraints: list = []
    if parsed.having_conditions:
        for i in range(bound):
            having_clauses = []
            for cond in parsed.having_conditions:
                z3_clause = _having_to_z3(cond, agg_values, i)
                if z3_clause is not None:
                    having_clauses.append(z3_clause)
            if having_clauses:
                # If row is in output but fails HAVING, it must not be in output
                extra_constraints.append(
                    Implies(Not(And(having_clauses)), Not(in_output[i]))
                )

    return QueryFormula(
        in_output=in_output,
        agg_values=agg_values,
        group_key_vars=group_key_vars,
        extra_constraints=extra_constraints,
        bound=bound,
    )


# ── Condition → Z3 helpers ───────────────────────────────────────────────────

def _condition_to_z3(
    cond: ParsedCondition,
    alias_map: dict[str, str],
    db: SymbolicDB,
    left_row: int,
    right_row: Optional[int],
    schema: SchemaModel,
) -> Optional[BoolRef]:
    """Convert a ParsedCondition to a Z3 boolean expression."""

    def resolve(alias):
        return alias_map.get(alias, alias)

    # When no alias specified, default to the FROM table (first in alias_map).
    # Sqlglot omits table_alias when the query has no explicit alias prefix
    # e.g. "WHERE balance > 0" instead of "WHERE a.balance > 0"
    if cond.table_alias is None:
        tname = list(alias_map.values())[0]
    else:
        tname = resolve(cond.table_alias)

    row_idx = right_row if (right_row is not None and tname != list(alias_map.values())[0]) else left_row

    if cond.col not in db.vars.get(tname, {}):
        return None  # unknown column — skip silently

    z3_var = db.get_var(tname, cond.col, row_idx)

    # Get column type from schema to coerce literals correctly
    table_obj = schema.get_table(tname)
    col_obj = table_obj.get_column(cond.col) if table_obj else None
    col_type = col_obj.col_type if col_obj else "INTEGER"

    if cond.value is None and cond.op not in ("is_null", "is_not_null"):
        return None

    def z3_val(v):
        if isinstance(v, str):
            int_val = db.intern_string(tname, cond.col, v)
            return IntVal(int_val)
        if col_type == "REAL":
            return RealVal(float(v))
        return IntVal(int(v))

    if cond.op == "eq":
        return z3_var == z3_val(cond.value)
    if cond.op == "neq":
        return z3_var != z3_val(cond.value)
    if cond.op == "gt":
        return z3_var > z3_val(cond.value)
    if cond.op == "gte":
        return z3_var >= z3_val(cond.value)
    if cond.op == "lt":
        return z3_var < z3_val(cond.value)
    if cond.op == "lte":
        return z3_var <= z3_val(cond.value)
    if cond.op == "is_null":
        col_obj = schema.get_table(tname) and schema.get_table(tname).get_column(cond.col)
        if col_obj and not col_obj.nullable:
            return BoolVal(False)
        return BoolVal(False)
    if cond.op == "is_not_null":
        return BoolVal(True)

    return None



def _having_to_z3(
    cond: ParsedCondition,
    agg_values: dict[str, list],
    group_idx: int,
) -> Optional[BoolRef]:
    """Convert a HAVING ParsedCondition to a Z3 clause for group group_idx."""

    # Find the relevant aggregate alias
    agg_alias = None
    for alias, vals in agg_values.items():
        # Match by aggregate type and column
        agg_alias = alias
        break   # V1: use first aggregate for HAVING (sufficient for common cases)

    if agg_alias is None or group_idx >= len(agg_values[agg_alias]):
        return None

    agg_var = agg_values[agg_alias][group_idx]
    val = cond.value

    if val is None:
        return None

    def z3_val(v):
        return RealVal(float(v)) if isinstance(v, float) else IntVal(int(v))

    op = cond.op.replace("having_", "")
    if op == "gt":  return agg_var > z3_val(val)
    if op == "gte": return agg_var >= z3_val(val)
    if op == "lt":  return agg_var < z3_val(val)
    if op == "lte": return agg_var <= z3_val(val)
    if op == "eq":  return agg_var == z3_val(val)

    return None
