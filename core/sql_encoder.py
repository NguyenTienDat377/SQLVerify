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
    # Column-vs-column predicate (e.g. WHERE b = a): the right-hand side is
    # another column rather than a literal. When rhs_col is set, `value` is
    # None and the comparison is between two cells under three-valued logic.
    rhs_table_alias: Optional[str] = None
    rhs_col: Optional[str] = None


@dataclass
class ParsedSelectExpr:
    """One expression in the SELECT list."""
    alias: str                    # output column name
    expr_type: str                # 'column' | 'sum' | 'count_star' | 'count_col'
    table_alias: Optional[str]    # source table alias
    col_name: Optional[str]       # source column name (None for COUNT(*))
    # COALESCE(agg, default): when set, a NULL aggregate result is replaced by
    # this default and the column becomes non-NULL. Discarding it (the old
    # behaviour) made COALESCE(SUM(x),0) indistinguishable from SUM(x).
    coalesce_default: Optional[object] = None


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
        # vars[table][col][row_idx] → Z3 value variable
        self.vars: dict[str, dict[str, list]] = {}
        # nulls[table][col][row_idx] → Bool: is this cell NULL?
        # This is the (b, v) pair encoding from the paper (§4.2): every symbolic
        # value is a pair where `b` marks NULL and `v` holds the non-NULL value.
        self.nulls: dict[str, dict[str, list[BoolRef]]] = {}
        # exists[table][row_idx] → Bool (= ¬Del(t) from the paper)
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
            self.nulls[table_name] = {}
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
                self.nulls[table_name][col.name] = [
                    Bool(f"{table_name}_{col.name}_null_{i}")
                    for i in range(self.bound)
                ]

    def get_var(self, table_name: str, col_name: str, row_idx: int):
        """Retrieve the Z3 value variable for table.column[row_idx]."""
        if table_name not in self.vars:
            raise KeyError(f"Table '{table_name}' not in symbolic DB.")
        if col_name not in self.vars[table_name]:
            raise KeyError(f"Column '{col_name}' not in table '{table_name}'.")
        return self.vars[table_name][col_name][row_idx]

    def get_null(self, table_name: str, col_name: str, row_idx: int) -> BoolRef:
        """Retrieve the Z3 NULL flag for table.column[row_idx]."""
        return self.nulls[table_name][col_name][row_idx]

    def cell(self, table_name: str, col_name: str, row_idx: int) -> tuple:
        """Return the (is_null, value) pair for table.column[row_idx]."""
        return (
            self.nulls[table_name][col_name][row_idx],
            self.vars[table_name][col_name][row_idx],
        )

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
        # Value-domain half-width. The paper uses an unbounded integer theory;
        # we keep a finite window for tractability but make it symmetric around
        # zero so that 0 and negative values (e.g. WHERE x = 0, balances) are
        # reachable. A domain that started at 1 made "equivalent" verdicts
        # unsound — divergences needing 0/negatives could never be found.
        span = self.bound * 4

        for table_name, table in self.schema.tables.items():
            exists = self.exists[table_name]

            for i in range(self.bound):
                row_exists = exists[i]

                for col in table.columns:
                    var = self.vars[table_name][col.name][i]
                    is_null = self.nulls[table_name][col.name][i]
                    # Bounds only constrain the value when the cell is non-NULL.
                    not_null = Not(is_null)

                    if col.col_type == "REAL":
                        bound_c = And(var >= RealVal(-span * 250), var <= RealVal(span * 250))
                    elif col.col_type in ("TEXT", "TIMESTAMP", "BOOLEAN"):
                        # Symbolic enum: small positive integer domain (interned).
                        bound_c = And(var >= 1, var <= self.bound * 3)
                    else:  # INTEGER
                        bound_c = And(var >= -span, var <= span)
                    constraints.append(Implies(And(row_exists, not_null), bound_c))

                    # NOT NULL constraint (IC-NN): non-nullable columns of an
                    # existing row may never be NULL.
                    if not col.nullable:
                        constraints.append(Implies(row_exists, not_null))

            # ── Primary key uniqueness (IC-PK) ────────────────────────────────
            # Paper rule (Fig. 8): two existing tuples must differ on AT LEAST
            # ONE primary key attribute, i.e. ¬(∧ tᵢ.aₖ = tⱼ.aₖ). Asserting
            # per-column inequality over-constrains composite keys — it forbids
            # rows that share one PK column even when another differs, silently
            # discarding valid databases (and thus valid counterexamples).
            pk_cols = [
                c for c in table.primary_key_columns
                if c in self.vars[table_name]
            ]
            if pk_cols:
                for i in range(self.bound):
                    for j in range(i + 1, self.bound):
                        differs = [
                            self.vars[table_name][c][i] != self.vars[table_name][c][j]
                            for c in pk_cols
                        ]
                        constraints.append(
                            Implies(And(exists[i], exists[j]), Or(differs))
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
                src_nulls = self.nulls[table_name][src_col_name]
                ref_vars = self.vars[ref_tname][ref_col_name]
                ref_nulls = self.nulls[ref_tname][ref_col_name]
                ref_exists = self.exists[ref_tname]

                for i in range(self.bound):
                    # A NULL foreign key is permitted in SQL (unless the column
                    # is also NOT NULL, which is enforced separately above).
                    # Otherwise the value must reference an existing, non-NULL key.
                    constraints.append(
                        Implies(
                            And(exists[i], Not(src_nulls[i])),
                            Or([
                                And(ref_exists[j], Not(ref_nulls[j]), src_vars[i] == ref_vars[j])
                                for j in range(self.bound)
                            ]),
                        )
                    )

        return constraints


# ── Query formula ────────────────────────────────────────────────────────────

@dataclass
class SymValue:
    """A symbolic value following the paper's (b, v) NULL encoding (§4.2)."""
    is_null: BoolRef    # Bool: True iff the value is NULL
    value: object       # Z3 ArithRef: the non-NULL value (meaningful when ¬is_null)


@dataclass
class OutputTuple:
    """One candidate row in a query's output bag.

    present  — Bool: is this tuple actually in the result (non-deleted)?
    cols     — the tuple's column values, positionally matching the SELECT list.
    """
    present: BoolRef
    cols: list[SymValue]


@dataclass
class QueryFormula:
    """
    Z3 encoding of a SELECT query's output as a *bag of symbolic tuples*.

    Rather than indexing by FROM-table row (which only works when both queries
    group by the same key), we materialise the output as a bounded list of
    OutputTuples and compare bags via multiplicity in equivalence.py — matching
    the paper's bag-equivalence formulation (Eqns 1–2).

    arity              — number of output columns (SELECT list length).
    col_aliases        — output column names, positional.
    extra_constraints  — auxiliary assertions (e.g. group-leader definitions);
                         no divergence assertions live here.
    """
    output: list[OutputTuple]
    arity: int
    col_aliases: list[str]
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
            if isinstance(right, exp.Column):
                rtbl = right.args.get("table")
                results.append(ParsedCondition(
                    op="eq",
                    table_alias=tbl.name if tbl else None,
                    col=left.name,
                    value=None,
                    rhs_table_alias=rtbl.name if rtbl else None,
                    rhs_col=right.name,
                ))
            else:
                results.append(ParsedCondition(
                    op="eq",
                    table_alias=tbl.name if tbl else None,
                    col=left.name,
                    value=_literal_value(right),
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
                if isinstance(right, exp.Column):
                    rtbl = right.args.get("table")
                    results.append(ParsedCondition(
                        op=op_str,
                        table_alias=tbl.name if tbl else None,
                        col=left.name,
                        value=None,
                        rhs_table_alias=rtbl.name if rtbl else None,
                        rhs_col=right.name,
                    ))
                else:
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
                        rhs_table_alias=cond.rhs_table_alias,
                        rhs_col=cond.rhs_col,
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

        # COALESCE(SUM(...), 0) — preserve the default so NULL→default is encoded.
        coalesce_default = None
        if isinstance(inner, exp.Coalesce):
            for arg in inner.find_all(exp.Literal):
                coalesce_default = _literal_value(arg)
                break
            if coalesce_default is None:
                coalesce_default = 0
            inner = inner.this  # unwrap to the aggregate inside

        if isinstance(inner, exp.Sum):
            agg_type, col_name, tbl_alias = _parse_aggregate(inner)
            return ParsedSelectExpr(
                alias=alias_name,
                expr_type="sum",
                table_alias=tbl_alias,
                col_name=col_name,
                coalesce_default=coalesce_default,
            )

        if isinstance(inner, exp.Count):
            agg_type, col_name, tbl_alias = _parse_aggregate(inner)
            return ParsedSelectExpr(
                alias=alias_name,
                expr_type=agg_type,   # 'count_star' or 'count_col'
                table_alias=tbl_alias,
                col_name=col_name,
                coalesce_default=coalesce_default,
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
            join_type = "RIGHT" 
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
    Encode a ParsedQuery into a QueryFormula (a bag of symbolic OutputTuples).

    The encoding follows the paper's structure: each output column is a
    NULL-aware (b, v) SymValue, predicates use three-valued logic, GROUP BY
    merges rows with equal keys, and the result is a bounded list of tuples
    whose multiplicities are compared for bag-equivalence in equivalence.py.

    Args:
        parsed:  Output of parse_query().
        db:      Shared SymbolicDB — must be the SAME instance for both queries
                 being compared in equivalence.py.
        schema:  The SchemaModel (used for alias → table name resolution).
    """
    bound = db.bound

    # ── Alias → real table name resolution ───────────────────────────────────
    alias_map: dict[str, str] = {parsed.from_alias: parsed.from_table}
    for join in parsed.joins:
        alias_map[join.alias] = join.table_name

    def resolve(alias: str) -> str:
        return alias_map.get(alias, alias)

    from_alias = parsed.from_alias
    has_join = bool(parsed.joins)
    join = parsed.joins[0] if has_join else None
    join_alias = join.alias if has_join else None
    join_tname = resolve(join_alias) if has_join else None
    is_left = has_join and join.join_type == "LEFT"
    is_right = has_join and join.join_type == "RIGHT"

    def cell(alias: str, col: str, i: int) -> tuple:
        return db.cell(resolve(alias), col, i)

    def row_exists(alias: str, i: int) -> BoolRef:
        return db.exists[resolve(alias)][i]

    # A predicate "touches" the join table when either side references it. Such
    # predicates need both row indices (i, j) and so are evaluated in the join
    # context (where_right); everything else stays on the FROM-table side.
    def refs_join(cond: ParsedCondition) -> bool:
        if not has_join:
            return False
        return cond.table_alias == join_alias or cond.rhs_table_alias == join_alias

    # ── WHERE filters (three-valued: keep a row only when the predicate is TRUE)
    def where_left(i: int) -> BoolRef:
        clauses = []
        for cond in parsed.where_conditions:
            if refs_join(cond):
                continue  # handled in the join context (where_right)
            if cond.table_alias not in (from_alias, None):
                continue
            c = _pred_true(cond, alias_map, db, i, None, schema)
            if c is not None:
                clauses.append(c)
        return And(clauses) if clauses else BoolVal(True)

    def where_right(i: int, j: int) -> BoolRef:
        if not has_join:
            return BoolVal(True)
        clauses = []
        for cond in parsed.where_conditions:
            if not refs_join(cond):
                continue
            c = _pred_true(cond, alias_map, db, i, j, schema)
            if c is not None:
                clauses.append(c)
        return And(clauses) if clauses else BoolVal(True)

    # ── JOIN match (three-valued ON equality: both keys non-NULL and equal) ──
    def join_match(i: int, j: int) -> BoolRef:
        ln, lv = cell(join.on_left_alias, join.on_left_col, i)
        rn, rv = cell(join.on_right_alias, join.on_right_col, j)
        return And(row_exists(join_alias, j), Not(ln), Not(rn), lv == rv,
                   where_right(i, j))

    def has_match_left(i: int) -> BoolRef:
        return Or([join_match(i, j) for j in range(bound)]) if has_join else BoolVal(True)
    
    def has_match_right(j: int) -> BoolRef:
        return Or([And(qualifies(i), join_match(i, j)) for i in range(bound)]) if has_join else BoolVal(True)

    def qualifies(i: int) -> BoolRef:
        return And(row_exists(from_alias, i), where_left(i))

    # ── Cell-accessor closures for aggregate contributions ───────────────────
    def cellfn_pair(i: int, j: int):
        def f(alias, col):
            t = resolve(alias or from_alias)
            idx = j if (has_join and t == join_tname) else i
            return db.cell(t, col, idx)
        return f

    def cellfn_single(i: int):
        def f(alias, col):
            return db.cell(resolve(alias or from_alias), col, i)
        return f

    def cellfn_nullext_left(i: int):
        def f(alias, col):
            t = resolve(alias or from_alias)
            if has_join and t == join_tname:
                return (BoolVal(True), IntVal(0))  # right side is NULL-extended
            return db.cell(t, col, i)
        return f
    
    def cellfn_nullext_right(j: int):
        def f(alias, col):
            t = resolve(alias or from_alias)
            if has_join and t != join_tname:  # FROM (left) side is NULL-extended
                return (BoolVal(True), IntVal(0))
            return db.cell(t, col, j)
        return f

    # ── Projection of one SELECT column to a SymValue ────────────────────────
    def proj_col(sel: ParsedSelectExpr, i: Optional[int], j: Optional[int],
                 null_ext: bool = False, null_ext_right: bool = False) -> SymValue:
        t = resolve(sel.table_alias or from_alias)
        if has_join and t == join_tname:
            if null_ext:
                return SymValue(BoolVal(True), IntVal(0))
            n, v = db.cell(t, sel.col_name, j)
            return SymValue(n, v)
        if null_ext_right:
            return SymValue(BoolVal(True), IntVal(0))
        n, v = db.cell(t, sel.col_name, i)
        return SymValue(n, v)

    # ── Aggregate over a list of contributions (active_bool, cell_fn) ────────
    def agg_value(sel: ParsedSelectExpr, contribs: list) -> SymValue:
        t = resolve(sel.table_alias or from_alias)
        col_obj = (schema.get_table(t).get_column(sel.col_name)
                   if (sel.col_name and schema.get_table(t)) else None)
        is_real = bool(col_obj and col_obj.col_type == "REAL")
        zero = RealVal(0) if is_real else IntVal(0)

        def coalesce(sv: SymValue) -> SymValue:
            # COALESCE(agg, default): replace a NULL result with the default.
            if sel.coalesce_default is None:
                return sv
            d = sel.coalesce_default
            dlit = RealVal(float(d)) if is_real else IntVal(int(d))
            return SymValue(BoolVal(False), If(sv.is_null, dlit, sv.value))

        if sel.expr_type == "count_star":
            total = (Sum([If(a, IntVal(1), IntVal(0)) for a, _ in contribs])
                     if contribs else IntVal(0))
            return coalesce(SymValue(BoolVal(False), total))

        if sel.expr_type == "count_col":
            # COUNT(col) ignores NULLs (now modelled explicitly).
            terms = []
            for a, cf in contribs:
                n, _ = cf(sel.table_alias, sel.col_name)
                terms.append(If(And(a, Not(n)), IntVal(1), IntVal(0)))
            return coalesce(SymValue(BoolVal(False), Sum(terms) if terms else IntVal(0)))

        if sel.expr_type == "sum":
            terms, live_terms = [], []
            for a, cf in contribs:
                n, v = cf(sel.table_alias, sel.col_name)
                live = And(a, Not(n))
                terms.append(If(live, v, zero))
                live_terms.append(live)
            total = Sum(terms) if terms else zero
            # SUM over an empty / all-NULL group is NULL in SQL.
            null_flag = Not(Or(live_terms)) if live_terms else BoolVal(True)
            return coalesce(SymValue(null_flag, total))

        return SymValue(BoolVal(True), zero)  # unreachable for known agg types

    col_aliases = [sel.alias for sel in parsed.select_exprs]
    arity = len(col_aliases)
    has_agg = any(s.expr_type in ("sum", "count_star", "count_col")
                  for s in parsed.select_exprs)
    has_group = bool(parsed.group_by)
    extra_constraints: list = []
    output: list[OutputTuple] = []

    def _having_present(base: BoolRef, col_by_alias: dict) -> BoolRef:
        clauses = []
        for cond in parsed.having_conditions:
            alias = _match_having_alias(cond, parsed.select_exprs)
            sv = col_by_alias.get(alias)
            if sv is None:
                continue
            hc = _having_pred(cond, sv)
            if hc is not None:
                clauses.append(hc)
        return And([base, *clauses]) if clauses else base

    if has_group:
        # ── GROUP BY: one output tuple per distinct key (Dedup + Eval). ──────
        def group_key_eq(i: int, g: int) -> BoolRef:
            clauses = []
            for (galias, gcol) in parsed.group_by:
                n1, v1 = cell(galias, gcol, i)
                n2, v2 = cell(galias, gcol, g)
                # NULLs group together (SQL groups NULL keys into one group).
                clauses.append(Or(And(n1, n2),
                                  And(Not(n1), Not(n2), v1 == v2)))
            return And(clauses) if clauses else BoolVal(True)

        for g in range(bound):
            # Row g is the leader of its group iff it qualifies and no earlier
            # qualifying row shares its key.
            earlier = [Not(And(qualifies(h), group_key_eq(h, g))) for h in range(g)]
            leader = And(qualifies(g), *earlier) if earlier else qualifies(g)

            contribs = []
            for i in range(bound):
                member = And(qualifies(i), group_key_eq(i, g))
                if has_join:
                    for j in range(bound):
                        contribs.append((And(member, join_match(i, j)), cellfn_pair(i, j)))
                    if is_left:
                        contribs.append((And(member, Not(has_match_left(i))), cellfn_nullext_left(i)))
                else:
                    contribs.append((member, cellfn_single(i)))

            # A group only appears if it actually has rows. For an INNER join
            # that means at least one match; for a LEFT join the null-extended
            # contribution keeps the group alive, and a join-free group always
            # contains its own leader row.
            nonempty = Or([a for a, _ in contribs]) if contribs else BoolVal(False)
            base_present = And(leader, nonempty)

            cols = []
            for sel in parsed.select_exprs:
                if sel.expr_type == "column":
                    cols.append(proj_col(sel, g, None))      # group-key value
                else:
                    cols.append(agg_value(sel, contribs))
            col_by_alias = {parsed.select_exprs[k].alias: cols[k] for k in range(arity)}
            output.append(OutputTuple(_having_present(base_present, col_by_alias), cols))

        if is_right:
            def _right_nullext_cell(alias: str, col: str, row_idx: int) -> tuple:
                t = resolve(alias)
                if t != join_tname:
                    return (BoolVal(True), IntVal(0))
                return db.cell(t, col, row_idx)

            def group_key_eq_right(j1: int, j2: int) -> BoolRef:
                clauses = []
                for (galias, gcol) in parsed.group_by:
                    n1, v1 = _right_nullext_cell(galias, gcol, j1)
                    n2, v2 = _right_nullext_cell(galias, gcol, j2)
                    clauses.append(Or(And(n1, n2), And(Not(n1), Not(n2), v1 == v2)))
                return And(clauses) if clauses else BoolVal(True)

            def qualifies_right(j: int) -> BoolRef:
                return And(row_exists(join_alias, j), Not(has_match_right(j)))

            for g in range(bound):
                earlier_r = [Not(And(qualifies_right(h), group_key_eq_right(h, g))) for h in range(g)]
                leader_r = And(qualifies_right(g), *earlier_r) if earlier_r else qualifies_right(g)

                contribs_r = []
                for j in range(bound):
                    member_r = And(qualifies_right(j), group_key_eq_right(j, g))
                    contribs_r.append((member_r, cellfn_nullext_right(j)))

                nonempty_r = Or([a for a, _ in contribs_r]) if contribs_r else BoolVal(False)
                base_present_r = And(leader_r, nonempty_r)

                cols_r = []
                for sel in parsed.select_exprs:
                    if sel.expr_type == "column":
                        cols_r.append(proj_col(sel, None, g, null_ext_right=True))
                    else:
                        cols_r.append(agg_value(sel, contribs_r))
                col_by_alias_r = {parsed.select_exprs[k].alias: cols_r[k] for k in range(arity)}
                output.append(OutputTuple(_having_present(base_present_r, col_by_alias_r), cols_r))

    elif has_agg:
        # ── Aggregate without GROUP BY: exactly one output row. ──────────────
        contribs = []
        for i in range(bound):
            if has_join:
                for j in range(bound):
                    contribs.append((And(qualifies(i), join_match(i, j)), cellfn_pair(i, j)))
                if is_left:
                    contribs.append((And(qualifies(i), Not(has_match_left(i))), cellfn_nullext_left(i)))
            else:
                contribs.append((qualifies(i), cellfn_single(i)))
        if is_right:
            for j in range(bound):
                contribs.append((And(row_exists(join_alias, j), Not(has_match_right(j))), cellfn_nullext_right(j)))

        cols = []
        for sel in parsed.select_exprs:
            if sel.expr_type == "column":
                cols.append(proj_col(sel, 0, 0))  # best-effort; bare col w/o GROUP BY
            else:
                cols.append(agg_value(sel, contribs))
        col_by_alias = {parsed.select_exprs[k].alias: cols[k] for k in range(arity)}
        # An ungrouped aggregate always returns one row; HAVING may drop it.
        output.append(OutputTuple(_having_present(BoolVal(True), col_by_alias), cols))

    else:
        # ── Plain projection: one tuple per qualifying row / matching pair. ──
        for i in range(bound):
            if not has_join:
                cols = [proj_col(sel, i, None) for sel in parsed.select_exprs]
                output.append(OutputTuple(qualifies(i), cols))
                continue
            for j in range(bound):
                cols = [proj_col(sel, i, j) for sel in parsed.select_exprs]
                output.append(OutputTuple(And(qualifies(i), join_match(i, j)), cols))
            if is_left:
                cols = [proj_col(sel, i, None, null_ext=True) for sel in parsed.select_exprs]
                output.append(OutputTuple(And(qualifies(i), Not(has_match_left(i))), cols))
        if is_right:
            for j in range(bound):
                cols = [proj_col(sel, None, j, null_ext_right=True) for sel in parsed.select_exprs]
                output.append(OutputTuple(And(row_exists(join_alias, j), Not(has_match_right(j))), cols))

    return QueryFormula(
        output=output,
        arity=arity,
        col_aliases=col_aliases,
        extra_constraints=extra_constraints,
        bound=bound,
    )


# ── Condition → Z3 helpers ───────────────────────────────────────────────────

def _pred_true(
    cond: ParsedCondition,
    alias_map: dict[str, str],
    db: SymbolicDB,
    left_row: int,
    right_row: Optional[int],
    schema: SchemaModel,
) -> Optional[BoolRef]:
    """
    Encode a predicate under three-valued logic and return a Z3 Bool that is
    TRUE iff the predicate evaluates to TRUE (a filter keeps a row only then;
    NULL and FALSE both fail). Returns None when the predicate can't be encoded.
    """
    table_names = list(alias_map.values())
    from_tname = table_names[0]

    def resolve(alias: Optional[str]) -> Optional[tuple[str, int]]:
        """Resolve an aliased column to (table_name, row_index), picking the row
        index the same way the join-match code does: the right-hand (j) row for
        the joined table, the left-hand (i) row for the FROM table."""
        tn = from_tname if alias is None else alias_map.get(alias, alias)
        if tn not in db.vars:
            return None
        idx = right_row if (right_row is not None and tn != from_tname) else left_row
        return tn, idx

    resolved = resolve(cond.table_alias)
    if resolved is None:
        return None
    tname, row_idx = resolved

    if cond.col not in db.vars.get(tname, {}):
        return None  # unknown column — skip silently

    is_null, val = db.cell(tname, cond.col, row_idx)

    # NULL checks are the only predicates that can yield TRUE on a NULL value.
    if cond.op == "is_null":
        return is_null
    if cond.op == "is_not_null":
        return Not(is_null)

    # ── Column-vs-column predicate (e.g. WHERE b = a) ────────────────────────
    # Under three-valued logic the comparison is TRUE only when both cells are
    # non-NULL and the underlying values relate as the operator requires. The
    # previous code returned None here (cond.value is None), silently dropping
    # the filter and making the encoded query unsoundly broad.
    if cond.rhs_col is not None:
        rhs_resolved = resolve(cond.rhs_table_alias)
        if rhs_resolved is None:
            return None
        rhs_tname, rhs_row_idx = rhs_resolved
        if cond.rhs_col not in db.vars.get(rhs_tname, {}):
            return None
        r_is_null, r_val = db.cell(rhs_tname, cond.rhs_col, rhs_row_idx)
        both_nn = And(Not(is_null), Not(r_is_null))
        if cond.op == "eq":  return And(both_nn, val == r_val)
        if cond.op == "neq": return And(both_nn, val != r_val)
        if cond.op == "gt":  return And(both_nn, val > r_val)
        if cond.op == "gte": return And(both_nn, val >= r_val)
        if cond.op == "lt":  return And(both_nn, val < r_val)
        if cond.op == "lte": return And(both_nn, val <= r_val)
        return None

    if cond.value is None:
        return None

    table_obj = schema.get_table(tname)
    col_obj = table_obj.get_column(cond.col) if table_obj else None
    col_type = col_obj.col_type if col_obj else "INTEGER"

    def lit(v):
        if isinstance(v, str):
            return IntVal(db.intern_string(tname, cond.col, v))
        if col_type == "REAL":
            return RealVal(float(v))
        return IntVal(int(v))

    rhs = lit(cond.value)
    notnull = Not(is_null)   # comparisons against a NULL operand are never TRUE

    if cond.op == "eq":  return And(notnull, val == rhs)
    if cond.op == "neq": return And(notnull, val != rhs)
    if cond.op == "gt":  return And(notnull, val > rhs)
    if cond.op == "gte": return And(notnull, val >= rhs)
    if cond.op == "lt":  return And(notnull, val < rhs)
    if cond.op == "lte": return And(notnull, val <= rhs)

    return None


def _match_having_alias(
    cond: ParsedCondition,
    select_exprs: list[ParsedSelectExpr],
) -> Optional[str]:
    """
    Resolve a HAVING condition to the SELECT alias whose aggregate it references.

    Previously the encoder always used the first aggregate alias, which produced
    wrong constraints whenever a query had more than one aggregate (e.g.
    SELECT SUM(x), COUNT(*) ... HAVING COUNT(*) > 2 would filter on SUM).
    """
    for sel in select_exprs:
        if sel.expr_type not in ("sum", "count_star", "count_col"):
            continue
        if cond.agg_type != sel.expr_type:
            continue
        # COUNT(*) has no column; SUM/COUNT(col) must match the referenced column.
        if sel.expr_type == "count_star" or cond.agg_col == sel.col_name:
            return sel.alias
    return None


def _having_pred(cond: ParsedCondition, sv: SymValue) -> Optional[BoolRef]:
    """Three-valued HAVING predicate over a group's aggregate SymValue.
    TRUE only when the aggregate is non-NULL and the comparison holds."""
    val = cond.value
    if val is None:
        return None
    rhs = RealVal(float(val)) if isinstance(val, float) else IntVal(int(val))
    notnull = Not(sv.is_null)
    op = cond.op.replace("having_", "")
    if op == "gt":  return And(notnull, sv.value > rhs)
    if op == "gte": return And(notnull, sv.value >= rhs)
    if op == "lt":  return And(notnull, sv.value < rhs)
    if op == "lte": return And(notnull, sv.value <= rhs)
    if op == "eq":  return And(notnull, sv.value == rhs)
    if op == "neq": return And(notnull, sv.value != rhs)
    return None
