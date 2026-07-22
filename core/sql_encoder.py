"""
core/sql_encoder.py

Parses a SELECT query and encodes it as Z3 formulas over a symbolic database.

Supported subset:
  SELECT   — column refs, SUM(col), COUNT(*), COUNT(col), COALESCE(agg, default)
  FROM     — single table with alias
  JOIN     — any number of INNER joins, OR one LEFT/RIGHT join, each with a
             simple ON equality condition. Outer joins cannot be combined with
             another join (multi-table joins must be INNER).
  WHERE    — boolean combinations (AND / OR / NOT) of comparisons (column vs
             literal, column vs column), IS NULL / IS NOT NULL, and
             IN (value-list). Evaluated under three-valued (Kleene) logic per
             the VeriEQL grammar (Fig. 4) and filter rule (Fig. 5).
  GROUP BY — one or more columns from any joined table
  HAVING   — aggregate comparisons (SUM(col) > v, COUNT(*) = v, ...), also
             combinable with AND / OR / NOT

Fail-closed policy: anything outside this subset raises ValueError instead of
being silently dropped. A verifier must never weaken the encoded query — a
dropped predicate or SELECT expression can turn a real divergence into a false
"equivalent" verdict, which is the one failure mode this tool cannot have.

Out of scope (all rejected with ValueError):
  FULL OUTER / CROSS joins, self-joins, outer joins combined with another join,
  IN (SELECT ...) subquery membership, BETWEEN / LIKE predicates, window
  functions, CTEs, subqueries, UNION, DISTINCT, LIMIT/OFFSET, string/timestamp
  ordering comparisons.
  (ORDER BY is accepted but ignored — equivalence is checked under bag
  semantics, where output order is immaterial.)

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

import itertools
import math
from dataclasses import dataclass
from typing import Optional

import sqlglot
import sqlglot.expressions as exp
from z3 import (
    And, Bool, BoolVal, If, Implies, Int, IntVal, Or, Real, RealVal,
    Sum, Not, BoolRef,
)

from core.models import SchemaModel


# ── Bound ────────────────────────────────────────────────────────────────────

DEFAULT_BOUND = 3  # symbolic rows per table
                    # increase for more coverage, decrease for speed


# ── Parsed query structures ──────────────────────────────────────────────────

@dataclass
class ParsedCondition:
    """A single WHERE / HAVING predicate."""
    op: str                          # 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte'
                                     # | 'is_null' | 'is_not_null'
                                     # | 'having_eq' | 'having_neq' | 'having_gt'
                                     # | 'having_gte' | 'having_lt' | 'having_lte'
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
class BoolNode:
    """A boolean combination of predicates, evaluated under three-valued
    (Kleene) logic — the paper's φ ∧ φ | φ ∨ φ | ¬φ productions (Fig. 4, p.7).

    op       — 'and' | 'or' | 'not'
    children — leaf ParsedConditions or nested BoolNodes ('not' has exactly one).

    A WHERE/HAVING clause parses to a tree whose root is a BoolNode or a bare
    ParsedCondition leaf (or None when absent). Encoded via _eval_tf().
    """
    op: str
    children: list


def _iter_pred_leaves(node):
    """Yield every ParsedCondition leaf in a predicate tree (for reference
    resolution and WHERE/HAVING validation). None trees yield nothing."""
    if node is None:
        return
    if isinstance(node, ParsedCondition):
        yield node
        return
    for ch in node.children:
        yield from _iter_pred_leaves(ch)


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
    join_type: str                # 'INNER' | 'LEFT' | 'RIGHT'
    on_left_alias: Optional[str]  # alias on left side of ON condition
    on_left_col: str
    on_right_alias: Optional[str] # alias on right side of ON condition
    on_right_col: str


@dataclass
class ParsedQuery:
    from_table: str
    from_alias: str
    joins: list[ParsedJoin]
    select_exprs: list[ParsedSelectExpr]
    group_by: list[tuple[Optional[str], str]]    # [(table_alias, col_name)]
    # Predicate trees (BoolNode | ParsedCondition leaf | None), not flat lists:
    # WHERE/HAVING may now be arbitrary AND/OR/NOT combinations.
    where_conditions: Optional[object]
    having_conditions: Optional[object]


# ── Symbolic database ────────────────────────────────────────────────────────

class SymbolicDB:
    """
    A bounded symbolic database: N rows per table, each column is a Z3 variable.

    TEXT and TIMESTAMP columns are encoded as Int (symbolic enum).
    REAL columns are encoded as Real.
    INTEGER / BOOLEAN columns are encoded as Int.

    String literals are interned to integers via intern_string(). The intern
    namespace is GLOBAL (one code per distinct string), not per-column:
    per-column interning let 'active' in one column and 'pending' in another
    share the same code, making column-to-column TEXT equality unsound.
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
        # global string interning: literal → int code (1-based)
        self._str_map: dict[str, int] = {}
        # largest |numeric literal| seen while encoding — the finite value
        # domain must cover it (see note_numeric_literal / domain_constraints)
        self._max_numeric_literal: float = 0.0
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

    def intern_string(self, value: str) -> int:
        """
        Map a string literal to a stable integer for Z3 equality checks.
        'active' → 1, 'inactive' → 2, etc. — one global namespace, so equal
        codes always mean equal strings across every table and column.
        """
        if value not in self._str_map:
            self._str_map[value] = len(self._str_map) + 1
        return self._str_map[value]

    def note_numeric_literal(self, value) -> None:
        """Record a numeric literal used in a query predicate.

        The finite value window must reach every literal the queries compare
        against: with a domain of [-12, 12], `x > 100` and `x >= 100` are
        vacuously "equivalent" because no symbolic cell can ever hold 100.
        """
        self._max_numeric_literal = max(self._max_numeric_literal, abs(float(value)))

    def domain_constraints(self) -> list:
        """
        Generate all Z3 constraints derived from the SchemaModel:
          - Column value bounds
          - Primary key uniqueness
          - Foreign key referential integrity
          - NOT NULL (implied by bounds when nullable=False)

        Call this AFTER encode_query() so the symbolic-enum domain can account
        for every interned string literal.
        """
        constraints = []
        # Value-domain half-width. The paper uses an unbounded integer theory;
        # we keep a finite window for tractability but make it symmetric around
        # zero so that 0 and negative values (e.g. WHERE x = 0, balances) are
        # reachable. A domain that started at 1 made "equivalent" verdicts
        # unsound — divergences needing 0/negatives could never be found.
        # The window is widened past every numeric literal the queries use,
        # otherwise e.g. `x > 100` vs `x >= 100` are vacuously equivalent.
        span = self.bound * 4 + math.ceil(self._max_numeric_literal)
        # Symbolic-enum domain must cover every interned literal plus head-room
        # for "fresh" values distinct from all literals.
        enum_max = self.bound * 3 + len(self._str_map)

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
                        bound_c = And(var >= 1, var <= enum_max)
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

_COMPARISON_OPS = {
    exp.EQ: "eq", exp.NEQ: "neq",
    exp.GT: "gt", exp.GTE: "gte",
    exp.LT: "lt", exp.LTE: "lte",
}

def _require_literal(node: exp.Expression) -> object:
    """Extract a Python int/float/str from a literal AST node, or raise.

    Fail-closed: an unsupported right-hand side (boolean, NULL, expression,
    function call, ...) must abort encoding rather than drop the predicate.
    """
    if isinstance(node, exp.Literal):
        if node.is_number:
            val = node.this
            return float(val) if "." in val else int(val)
        return node.this   # string (without quotes)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and node.this.is_number:
        val = node.this.this
        return -(float(val) if "." in val else int(val))
    if isinstance(node, exp.Null):
        raise ValueError(
            f"Comparison to NULL ({node.sql()}) is always NULL in SQL — "
            "use IS NULL / IS NOT NULL instead."
        )
    raise ValueError(f"Unsupported literal in predicate: {node.sql()}")


def _parse_predicate(node: exp.Expression):
    """Parse a WHERE / HAVING AST node into a three-valued predicate tree.

    Returns a BoolNode ('and' | 'or' | 'not') or a leaf ParsedCondition (or
    None for an absent clause). Follows the paper's predicate grammar (Fig. 4,
    p.7): φ ::= b | Null | A ⊙ A | IsNull(E) | E⃗ ∈ Q | φ ∧ φ | φ ∨ φ | ¬φ —
    so AND / OR / NOT and comparisons are first-class. `IN (v1, v2, ...)` over a
    value/column list is sugar for a disjunction of equalities (A=v1 ∨ A=v2 ∨…);
    `IN (SELECT ...)` (the paper's E⃗ ∈ Q semi-join) is still rejected.

    Raises ValueError on anything outside the supported subset — never drops a
    predicate silently (a dropped predicate can turn a real divergence into a
    false 'equivalent').
    """
    if node is None:
        return None

    if isinstance(node, exp.Paren):
        return _parse_predicate(node.this)

    if isinstance(node, exp.And):
        return _bool_node("and", node.left, node.right)

    if isinstance(node, exp.Or):
        return _bool_node("or", node.left, node.right)

    if isinstance(node, exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        child = _parse_predicate(inner)
        if child is None:
            raise ValueError(f"Unsupported negation: {node.sql()}")
        return BoolNode(op="not", children=[child])

    if isinstance(node, exp.In):
        return _parse_in(node)

    # Comparison operators: col vs literal, col vs col, aggregate vs literal
    for node_type, op_str in _COMPARISON_OPS.items():
        if isinstance(node, node_type):
            return _parse_comparison(node, op_str)

    # IS NULL. (IS NOT NULL arrives as Not(Is(...)) and is handled above.)
    if isinstance(node, exp.Is):
        col_node = node.this
        if isinstance(col_node, exp.Column) and isinstance(node.expression, exp.Null):
            tbl = col_node.args.get("table")
            return ParsedCondition(
                op="is_null",
                table_alias=tbl.name if tbl else None,
                col=col_node.name,
                value=None,
            )
        raise ValueError(f"Unsupported IS predicate: {node.sql()}")

    raise ValueError(
        f"Unsupported WHERE/HAVING construct: {node.sql()} — supported: "
        "AND / OR / NOT, comparisons, IN (value-list), IS [NOT] NULL, and "
        "aggregate comparisons (IN (SELECT ...) subqueries are not yet supported)."
    )


def _bool_node(op: str, left: exp.Expression, right: exp.Expression) -> BoolNode:
    """Build a BoolNode for a binary AND/OR, flattening nested same-op children
    so `a AND b AND c` becomes one 3-child node rather than a lopsided tree."""
    children = []
    for side in (_parse_predicate(left), _parse_predicate(right)):
        if isinstance(side, BoolNode) and side.op == op:
            children.extend(side.children)
        else:
            children.append(side)
    return BoolNode(op=op, children=children)


def _parse_comparison(node: exp.Expression, op_str: str) -> ParsedCondition:
    """Parse a single comparison into a leaf ParsedCondition: column vs literal,
    column vs column, or aggregate vs literal (HAVING)."""
    left, right = node.this, node.expression

    if isinstance(left, exp.Column):
        tbl = left.args.get("table")
        if isinstance(right, exp.Column):
            rtbl = right.args.get("table")
            return ParsedCondition(
                op=op_str,
                table_alias=tbl.name if tbl else None,
                col=left.name,
                value=None,
                rhs_table_alias=rtbl.name if rtbl else None,
                rhs_col=right.name,
            )
        return ParsedCondition(
            op=op_str,
            table_alias=tbl.name if tbl else None,
            col=left.name,
            value=_require_literal(right),
        )

    # Aggregate comparison in HAVING (SUM(x) > v, COUNT(*) = v, ...)
    if isinstance(left, (exp.Sum, exp.Count)):
        agg_type, agg_col, agg_tbl = _parse_aggregate(left)
        return ParsedCondition(
            op=f"having_{op_str}",
            table_alias=None,
            col=None,
            value=_require_literal(right),
            agg_type=agg_type,
            agg_col=agg_col,
            agg_table_alias=agg_tbl,
        )

    raise ValueError(f"Unsupported comparison: {node.sql()}")


def _parse_in(node: exp.In):
    """Desugar `col IN (v1, v2, ...)` into `col = v1 OR col = v2 OR ...`.

    Each list item may be a literal or another column (reusing the col-vs-col
    equality leaf). `col IN (SELECT ...)` — the paper's E⃗ ∈ Q membership over a
    subquery — is a semi-join and is not yet supported; rejected fail-closed.
    """
    if node.args.get("query") is not None:
        raise ValueError("IN (SELECT ...) subqueries are not yet supported.")

    left = node.this
    if not isinstance(left, exp.Column):
        raise ValueError(f"IN must test a column: {node.sql()}")

    items = node.expressions or []
    if not items:
        raise ValueError(f"Empty IN list: {node.sql()}")
    if any(isinstance(it, (exp.Select, exp.Subquery)) for it in items):
        raise ValueError("IN (SELECT ...) subqueries are not yet supported.")

    tbl = left.args.get("table")
    talias = tbl.name if tbl else None
    children = []
    for it in items:
        if isinstance(it, exp.Column):
            rtbl = it.args.get("table")
            children.append(ParsedCondition(
                op="eq", table_alias=talias, col=left.name, value=None,
                rhs_table_alias=rtbl.name if rtbl else None, rhs_col=it.name,
            ))
        else:
            children.append(ParsedCondition(
                op="eq", table_alias=talias, col=left.name,
                value=_require_literal(it),
            ))
    if len(children) == 1:
        return children[0]
    return BoolNode(op="or", children=children)


def _parse_aggregate(node: exp.Expression) -> tuple[str, Optional[str], Optional[str]]:
    """
    Return (agg_type, col_name, table_alias) from a Sum or Count AST node.
    agg_type: 'sum' | 'count_star' | 'count_col'
    """
    if isinstance(node, exp.Sum):
        arg = node.this
        if not isinstance(arg, exp.Column):
            raise ValueError(
                f"Unsupported aggregate argument: {node.sql()} "
                "(V1 supports SUM over a single column)."
            )
        tbl = arg.args.get("table")
        return "sum", arg.name, (tbl.name if tbl else None)

    if isinstance(node, exp.Count):
        arg = node.this
        if isinstance(arg, exp.Star):
            return "count_star", None, None
        if isinstance(arg, exp.Distinct):
            raise ValueError("COUNT(DISTINCT ...) is not supported in V1.")
        if not isinstance(arg, exp.Column):
            raise ValueError(
                f"Unsupported aggregate argument: {node.sql()} "
                "(V1 supports COUNT(*) or COUNT over a single column)."
            )
        tbl = arg.args.get("table")
        return "count_col", arg.name, (tbl.name if tbl else None)

    raise ValueError(f"Unsupported aggregate: {node.sql()}")


def _parse_select_expr(node: exp.Expression) -> ParsedSelectExpr:
    """Extract a ParsedSelectExpr from one SELECT list item, or raise.

    Fail-closed: skipping an unsupported expression would change the query's
    output arity/content and could yield a false 'equivalent' verdict.
    """
    alias_name: Optional[str] = None
    inner = node
    if isinstance(node, exp.Alias):
        alias_name = node.alias
        inner = node.this

    # Bare column: SELECT a.account_id [AS alias]
    if isinstance(inner, exp.Column):
        tbl = inner.args.get("table")
        return ParsedSelectExpr(
            alias=alias_name or inner.name,
            expr_type="column",
            table_alias=tbl.name if tbl else None,
            col_name=inner.name,
        )

    # COALESCE(SUM(...), default) — preserve the default so NULL→default is encoded.
    coalesce_default = None
    if isinstance(inner, exp.Coalesce):
        rest = inner.expressions
        if len(rest) != 1:
            raise ValueError(
                f"Unsupported COALESCE: {node.sql()} "
                "(V1 supports COALESCE(aggregate, literal))."
            )
        coalesce_default = _require_literal(rest[0])
        inner = inner.this  # unwrap to the aggregate inside

    if isinstance(inner, (exp.Sum, exp.Count)):
        agg_type, col_name, tbl_alias = _parse_aggregate(inner)
        return ParsedSelectExpr(
            alias=alias_name or node.sql(),
            expr_type=agg_type,
            table_alias=tbl_alias,
            col_name=col_name,
            coalesce_default=coalesce_default,
        )

    raise ValueError(
        f"Unsupported SELECT expression: {node.sql()} — "
        "V1 supports plain columns, SUM(col), COUNT(*), COUNT(col), "
        "and COALESCE(aggregate, literal). SELECT * is not supported; "
        "list columns explicitly."
    )


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

    def arg(name: str):
        # sqlglot 30.x renamed reserved-word arg keys ('from' → 'from_', etc.)
        return stmt.args.get(name) or stmt.args.get(name + "_")

    # ── Whole-query constructs outside the V1 subset ─────────────────────────
    if arg("with"):
        raise ValueError("CTEs (WITH ...) are not supported in V1.")
    if arg("distinct"):
        raise ValueError("SELECT DISTINCT is not supported in V1.")
    if arg("limit"):
        raise ValueError("LIMIT is not supported in V1.")
    if arg("offset"):
        raise ValueError("OFFSET is not supported in V1.")
    if stmt.find(exp.Window):
        raise ValueError("Window functions are not supported in V1.")
    if any(s is not stmt for s in stmt.find_all(exp.Select)):
        raise ValueError("Subqueries are not supported in V1.")
    # ORDER BY is ignored: equivalence is checked under bag semantics.

    # ── FROM table ───────────────────────────────────────────────────────────
    from_clause = arg("from")
    if from_clause is None:
        raise ValueError("Query has no FROM clause.")

    from_table_node = from_clause.this
    if not isinstance(from_table_node, exp.Table):
        raise ValueError(
            "FROM must reference a plain table (derived tables / subqueries "
            "are not supported in V1)."
        )

    from_table = from_table_node.name
    from_alias = from_table_node.alias or from_table

    # ── JOINs (V1: max one join) ─────────────────────────────────────────────
    joins: list[ParsedJoin] = []
    for join_node in stmt.find_all(exp.Join):
        tbl = join_node.this
        if not isinstance(tbl, exp.Table):
            raise ValueError("JOIN must reference a plain table in V1.")

        kind = join_node.args.get("kind")   # 'INNER' / 'CROSS' string or None
        side = join_node.args.get("side")   # 'LEFT' / 'RIGHT' / 'FULL' or None

        if side == "LEFT":
            join_type = "LEFT"
        elif side == "RIGHT":
            join_type = "RIGHT"
        elif side == "FULL":
            raise ValueError("FULL OUTER JOIN is not supported in V1.")
        elif kind == "CROSS":
            raise ValueError("CROSS JOIN is not supported in V1.")
        else:
            join_type = "INNER"   # default: INNER

        on = join_node.args.get("on")
        while isinstance(on, exp.Paren):
            on = on.this
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
            on_left_alias=left_tbl.name if left_tbl else None,
            on_left_col=left_col.name,
            on_right_alias=right_tbl.name if right_tbl else None,
            on_right_col=right_col.name,
        ))

    # Multiple joins are allowed when every join is INNER (see encode_query's
    # dispatch). An outer join combined with any other join is rejected there —
    # multi-table joins must be INNER in this version.

    # ── SELECT expressions ───────────────────────────────────────────────────
    select_exprs = [_parse_select_expr(sel) for sel in stmt.expressions]
    if not select_exprs:
        raise ValueError("No SELECT expressions found.")

    # ── GROUP BY ─────────────────────────────────────────────────────────────
    group_by: list[tuple[Optional[str], str]] = []
    group_node = arg("group")
    if group_node:
        for g_expr in group_node.expressions:
            if not isinstance(g_expr, exp.Column):
                raise ValueError(
                    f"Unsupported GROUP BY expression: {g_expr.sql()} "
                    "(V1 supports plain columns only)."
                )
            tbl = g_expr.args.get("table")
            group_by.append((tbl.name if tbl else None, g_expr.name))

    # ── WHERE ────────────────────────────────────────────────────────────────
    where_conditions = None
    where_node = arg("where")
    if where_node:
        where_conditions = _parse_predicate(where_node.this)
        if any(c.op.startswith("having_") for c in _iter_pred_leaves(where_conditions)):
            raise ValueError("Aggregate comparisons belong in HAVING, not WHERE.")

    # ── HAVING ───────────────────────────────────────────────────────────────
    having_conditions = None
    having_node = arg("having")
    if having_node:
        having_conditions = _parse_predicate(having_node.this)
        for cond in _iter_pred_leaves(having_conditions):
            if cond.agg_type is None:
                raise ValueError(
                    "V1 supports only aggregate comparisons in HAVING "
                    "(e.g. HAVING COUNT(*) > 1)."
                )

    return ParsedQuery(
        from_table=from_table,
        from_alias=from_alias,
        joins=joins,
        select_exprs=select_exprs,
        group_by=group_by,
        where_conditions=where_conditions,
        having_conditions=having_conditions,
    )


# ── Alias / column resolution ────────────────────────────────────────────────

def _resolve_column(
    alias: Optional[str],
    col: str,
    alias_map: dict[str, str],
    db: SymbolicDB,
    ctx: str,
) -> str:
    """Resolve (alias, column) to a definite table alias, fail-closed.

    Unqualified columns are looked up across every table in the query; an
    unknown or ambiguous column raises instead of being silently attributed
    to the FROM table (the old behaviour, which dropped predicates on
    unqualified join-table columns).
    """
    if alias is not None:
        tname = alias_map.get(alias)
        if tname is None:
            raise ValueError(f"Unknown table alias '{alias}' in {ctx}.")
        if col not in db.vars[tname]:
            raise ValueError(f"Column '{col}' does not exist in table '{tname}' ({ctx}).")
        return alias
    hits = [a for a, t in alias_map.items() if col in db.vars[t]]
    if not hits:
        raise ValueError(f"Column '{col}' not found in any table referenced by the query ({ctx}).")
    if len(hits) > 1:
        raise ValueError(f"Column '{col}' is ambiguous in {ctx}; qualify it with a table alias.")
    return hits[0]


def _resolve_references(parsed: ParsedQuery, alias_map: dict[str, str], db: SymbolicDB) -> None:
    """Resolve and validate every column reference in the query, in place."""
    for join in parsed.joins:
        join.on_left_alias = _resolve_column(
            join.on_left_alias, join.on_left_col, alias_map, db, "JOIN ON")
        join.on_right_alias = _resolve_column(
            join.on_right_alias, join.on_right_col, alias_map, db, "JOIN ON")

    for sel in parsed.select_exprs:
        if sel.col_name is not None:   # column / sum / count_col
            sel.table_alias = _resolve_column(
                sel.table_alias, sel.col_name, alias_map, db, "SELECT")

    parsed.group_by = [
        (_resolve_column(a, c, alias_map, db, "GROUP BY"), c)
        for (a, c) in parsed.group_by
    ]

    for cond in (*_iter_pred_leaves(parsed.where_conditions),
                 *_iter_pred_leaves(parsed.having_conditions)):
        if cond.col is not None:
            cond.table_alias = _resolve_column(
                cond.table_alias, cond.col, alias_map, db, "WHERE/HAVING")
        if cond.rhs_col is not None:
            cond.rhs_table_alias = _resolve_column(
                cond.rhs_table_alias, cond.rhs_col, alias_map, db, "WHERE/HAVING")
        if cond.agg_col is not None:
            cond.agg_table_alias = _resolve_column(
                cond.agg_table_alias, cond.agg_col, alias_map, db, "HAVING")


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

    JOIN semantics: the ON condition and the WHERE clause are kept strictly
    separate. For outer joins the distinction is load-bearing — a filter in
    WHERE eliminates null-extended rows (making LEFT JOIN behave like INNER),
    while the same filter in ON merely restricts which rows match. Folding
    WHERE into the join match (the old behaviour) got this wrong in both
    directions.

    Args:
        parsed:  Output of parse_query().
        db:      Shared SymbolicDB — must be the SAME instance for both queries
                 being compared in equivalence.py.
        schema:  The SchemaModel (used for alias → table name resolution).
    """
    bound = db.bound

    # ── Alias → real table name resolution & fail-closed validation ──────────
    alias_map: dict[str, str] = {parsed.from_alias: parsed.from_table}
    for join in parsed.joins:
        alias_map[join.alias] = join.table_name

    for tname in alias_map.values():
        if tname not in db.vars:
            raise ValueError(f"Table '{tname}' not found in the schema.")

    _resolve_references(parsed, alias_map, db)

    def resolve(alias: str) -> str:
        return alias_map[alias]

    from_alias = parsed.from_alias
    from_tname = parsed.from_table

    # Self-join guard: SymbolicDB rows are keyed by real table name, so two
    # aliases of the same table would unsoundly share the same symbolic rows.
    if len(set(alias_map.values())) != len(alias_map):
        raise ValueError("Self-joins are not supported.")

    # ── Dispatch ─────────────────────────────────────────────────────────────
    # INNER-only joins (including no join at all) use the N-table join-bag path
    # below. A single LEFT/RIGHT join uses the dedicated outer-join path (its
    # null-extension and ON-vs-WHERE handling are load-bearing — left unchanged).
    # An outer join combined with any other join is rejected: multi-table joins
    # must be INNER in this version.
    outer_joins = [j for j in parsed.joins if j.join_type in ("LEFT", "RIGHT")]
    inner_only = not outer_joins
    if outer_joins and len(parsed.joins) > 1:
        raise ValueError(
            "Outer joins are supported only as a single join; multi-table "
            "joins must be INNER in this version."
        )

    # Two-table outer-join context (only meaningful on the single-outer path).
    has_join = bool(parsed.joins)
    join = parsed.joins[0] if has_join else None
    join_alias = join.alias if has_join else None
    join_tname = resolve(join_alias) if has_join else None
    is_left = has_join and join.join_type == "LEFT"
    is_right = has_join and join.join_type == "RIGHT"

    # ── Cell accessors: map (alias, col) → (is_null, value) per row context ──
    def cellfn_single(i: int):
        def f(alias, col):
            return db.cell(resolve(alias), col, i)
        return f

    def cellfn_pair(i: int, j: int):
        def f(alias, col):
            t = resolve(alias)
            return db.cell(t, col, j if t == join_tname else i)
        return f

    def cellfn_nullext_left(i: int):
        def f(alias, col):
            t = resolve(alias)
            if t == join_tname:
                return (BoolVal(True), IntVal(0))  # right side is NULL-extended
            return db.cell(t, col, i)
        return f

    def cellfn_nullext_right(j: int):
        def f(alias, col):
            t = resolve(alias)
            if t != join_tname:  # FROM (left) side is NULL-extended
                return (BoolVal(True), IntVal(0))
            return db.cell(t, col, j)
        return f

    # ── WHERE: evaluated on the post-join tuple (three-valued: TRUE keeps) ───
    # Paper Fig. 5: σ_φ(Q) = filter(Q, λx.[[φ]]_x = ⊤) — a row survives iff the
    # predicate's `is_true` half holds (NULL and FALSE both drop it).
    def where_all(cellfn) -> BoolRef:
        if parsed.where_conditions is None:
            return BoolVal(True)
        t, _ = _eval_tf(
            parsed.where_conditions,
            lambda cond: _pred_tf(cond, alias_map, db, schema, cellfn),
        )
        return t

    # ── INNER join bag: one entry per combination of rows across all tables ──
    def build_inner_join_bag() -> list[tuple[BoolRef, object]]:
        """Materialise the inner (and no-join) result as a bag of join rows.

        Each entry is (present, cellfn): present holds iff every participating
        row exists, every ON equality holds under three-valued logic, and the
        WHERE clause keeps the combined tuple. For INNER joins ON and WHERE are
        interchangeable, so folding WHERE in here is sound. Size is bound^n over
        the n participating tables.
        """
        aliases = [from_alias] + [j.alias for j in parsed.joins]
        bag: list[tuple[BoolRef, object]] = []
        for combo in itertools.product(range(bound), repeat=len(aliases)):
            idx = dict(zip(aliases, combo))

            def cellfn(a, c, _idx=idx):
                return db.cell(resolve(a), c, _idx[a])

            clauses = [db.exists[resolve(a)][idx[a]] for a in aliases]
            for j in parsed.joins:
                ln, lv = cellfn(j.on_left_alias, j.on_left_col)
                rn, rv = cellfn(j.on_right_alias, j.on_right_col)
                clauses.append(And(Not(ln), Not(rn), lv == rv))
            clauses.append(where_all(cellfn))
            bag.append((And(clauses), cellfn))
        return bag

    # ── ON condition only (three-valued equality); WHERE applies after ───────
    def on_match(i: int, j: int) -> BoolRef:
        cf = cellfn_pair(i, j)
        ln, lv = cf(join.on_left_alias, join.on_left_col)
        rn, rv = cf(join.on_right_alias, join.on_right_col)
        return And(db.exists[join_tname][j], Not(ln), Not(rn), lv == rv)

    # ── Memoised row/pair survival predicates ────────────────────────────────
    _pair_cache: dict[tuple[int, int], BoolRef] = {}
    _nl_cache: dict[int, BoolRef] = {}
    _nr_cache: dict[int, BoolRef] = {}
    _single_cache: dict[int, BoolRef] = {}

    def pair_present(i: int, j: int) -> BoolRef:
        """Matched pair (i, j) survives: both rows exist, ON holds, WHERE holds."""
        if (i, j) not in _pair_cache:
            _pair_cache[(i, j)] = And(
                db.exists[from_tname][i], on_match(i, j), where_all(cellfn_pair(i, j)))
        return _pair_cache[(i, j)]

    def has_match_left(i: int) -> BoolRef:
        return Or([on_match(i, j) for j in range(bound)])

    def has_match_right(j: int) -> BoolRef:
        return Or([And(db.exists[from_tname][i], on_match(i, j)) for i in range(bound)])

    def nullext_left_present(i: int) -> BoolRef:
        """LEFT-join null-extended row for unmatched FROM row i. WHERE is
        evaluated with the join side NULL — so WHERE r.x = 5 kills the row
        (LEFT≡INNER) while WHERE r.x IS NULL keeps it (anti-join)."""
        if i not in _nl_cache:
            _nl_cache[i] = And(
                db.exists[from_tname][i], Not(has_match_left(i)),
                where_all(cellfn_nullext_left(i)))
        return _nl_cache[i]

    def nullext_right_present(j: int) -> BoolRef:
        """RIGHT-join null-extended row for unmatched join row j (no existing
        FROM row matches the ON condition; WHERE plays no part in matching)."""
        if j not in _nr_cache:
            _nr_cache[j] = And(
                db.exists[join_tname][j], Not(has_match_right(j)),
                where_all(cellfn_nullext_right(j)))
        return _nr_cache[j]

    def single_present(i: int) -> BoolRef:
        if i not in _single_cache:
            _single_cache[i] = And(db.exists[from_tname][i], where_all(cellfn_single(i)))
        return _single_cache[i]

    # ── Projection of one SELECT column through a cell accessor ──────────────
    def proj(sel: ParsedSelectExpr, cellfn) -> SymValue:
        n, v = cellfn(sel.table_alias, sel.col_name)
        return SymValue(n, v)

    # ── Aggregate over a list of contributions (active_bool, cell_fn) ────────
    def agg_value(sel: ParsedSelectExpr, contribs: list) -> SymValue:
        is_real = False
        if sel.col_name is not None:
            t = resolve(sel.table_alias)
            col_obj = schema.get_table(t).get_column(sel.col_name) if schema.get_table(t) else None
            is_real = bool(col_obj and col_obj.col_type == "REAL")
        zero = RealVal(0) if is_real else IntVal(0)

        def coalesce(sv: SymValue) -> SymValue:
            # COALESCE(agg, default): replace a NULL result with the default.
            if sel.coalesce_default is None:
                return sv
            d = sel.coalesce_default
            db.note_numeric_literal(d)
            dlit = RealVal(float(d)) if is_real else IntVal(int(d))
            return SymValue(BoolVal(False), If(sv.is_null, dlit, sv.value))

        if sel.expr_type == "count_star":
            total = (Sum([If(a, IntVal(1), IntVal(0)) for a, _ in contribs])
                     if contribs else IntVal(0))
            return coalesce(SymValue(BoolVal(False), total))

        if sel.expr_type == "count_col":
            # COUNT(col) ignores NULLs (modelled explicitly).
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

        raise ValueError(f"Unsupported aggregate type: {sel.expr_type}")

    def having_clauses(contribs: list) -> list[BoolRef]:
        """Encode the HAVING predicate tree to its `is_true` Z3 Bool, returned
        as a 0-or-1 element list so callers can splat it into an And().

        HAVING aggregates are computed fresh from the group's contributions
        rather than matched to SELECT aliases — a HAVING aggregate need not
        appear in the SELECT list, and matching by alias would wrongly reuse a
        COALESCEd value. AND/OR/NOT over aggregate comparisons is supported via
        the same three-valued evaluator as WHERE."""
        if parsed.having_conditions is None:
            return []

        def leaf_tf(cond: ParsedCondition):
            if isinstance(cond.value, (int, float)):
                db.note_numeric_literal(cond.value)
            synth = ParsedSelectExpr(
                alias="_having",
                expr_type=cond.agg_type,
                table_alias=cond.agg_table_alias,
                col_name=cond.agg_col,
            )
            return _having_tf(cond, agg_value(synth, contribs))

        t, _ = _eval_tf(parsed.having_conditions, leaf_tf)
        return [t]

    col_aliases = [sel.alias for sel in parsed.select_exprs]
    arity = len(col_aliases)
    has_agg = any(s.expr_type in ("sum", "count_star", "count_col")
                  for s in parsed.select_exprs)
    has_group = bool(parsed.group_by)
    extra_constraints: list = []
    output: list[OutputTuple] = []

    # ── INNER path (0..N joins): build the join bag, then project/aggregate ──
    if inner_only:
        bag = build_inner_join_bag()

        if has_group:
            # GROUP BY keys (and bare SELECT columns) may come from ANY joined
            # table — dedup runs over join-bag rows, not FROM rows.
            gk_set = {(galias, gcol) for (galias, gcol) in parsed.group_by}
            for sel in parsed.select_exprs:
                if sel.expr_type == "column" and \
                        (sel.table_alias, sel.col_name) not in gk_set:
                    # A bare column not in GROUP BY is ambiguous (invalid in
                    # standard SQL); reject rather than invent a value.
                    raise ValueError(
                        f"Non-aggregated SELECT column '{sel.col_name}' must "
                        "appear in GROUP BY."
                    )

            def group_key_eq(cf_a, cf_b) -> BoolRef:
                clauses = []
                for (galias, gcol) in parsed.group_by:
                    n1, v1 = cf_a(galias, gcol)
                    n2, v2 = cf_b(galias, gcol)
                    # NULLs group together (SQL groups NULL keys into one group).
                    clauses.append(Or(And(n1, n2),
                                      And(Not(n1), Not(n2), v1 == v2)))
                return And(clauses) if clauses else BoolVal(True)

            for g, (g_present, g_cellfn) in enumerate(bag):
                # Row g leads its group iff it is present and no earlier present
                # row shares its key.
                earlier = [Not(And(bag[h][0], group_key_eq(bag[h][1], g_cellfn)))
                           for h in range(g)]
                base_present = And(g_present, *earlier) if earlier else g_present
                contribs = [(And(group_key_eq(cf, g_cellfn), pres), cf)
                            for (pres, cf) in bag]
                cols = []
                for sel in parsed.select_exprs:
                    if sel.expr_type == "column":
                        n, v = g_cellfn(sel.table_alias, sel.col_name)
                        cols.append(SymValue(n, v))
                    else:
                        cols.append(agg_value(sel, contribs))
                present = And(base_present, *having_clauses(contribs)) \
                    if parsed.having_conditions else base_present
                output.append(OutputTuple(present, cols))

        elif has_agg:
            # Aggregate without GROUP BY: exactly one output row.
            for sel in parsed.select_exprs:
                if sel.expr_type == "column":
                    raise ValueError(
                        f"Bare column '{sel.alias}' mixed with aggregates "
                        "requires GROUP BY."
                    )
            contribs = [(pres, cf) for (pres, cf) in bag]
            cols = [agg_value(sel, contribs) for sel in parsed.select_exprs]
            present = And(having_clauses(contribs)) \
                if parsed.having_conditions else BoolVal(True)
            output.append(OutputTuple(present, cols))

        else:
            # Plain projection: one candidate tuple per join-bag row.
            if parsed.having_conditions:
                raise ValueError(
                    "HAVING without GROUP BY or aggregates is not supported.")
            for (pres, cf) in bag:
                cols = [proj(sel, cf) for sel in parsed.select_exprs]
                output.append(OutputTuple(pres, cols))

        return QueryFormula(
            output=output,
            arity=arity,
            col_aliases=col_aliases,
            extra_constraints=extra_constraints,
            bound=bound,
        )

    # ── Single LEFT/RIGHT outer-join path (unchanged) ────────────────────────
    if has_group:
        # ── GROUP BY: one output tuple per distinct key (Dedup + Eval). ──────
        # V1 restriction: group keys (and bare SELECT columns) must come from
        # the FROM table — the dedup machinery indexes groups by FROM rows.
        for (galias, gcol) in parsed.group_by:
            if resolve(galias) != from_tname:
                raise ValueError(
                    "V1 supports GROUP BY only on columns of the FROM table; "
                    f"'{galias}.{gcol}' belongs to the joined table. "
                    "Swap the join direction to group by that table's columns."
                )
        group_key_cols = {gcol for (_, gcol) in parsed.group_by}
        for sel in parsed.select_exprs:
            if sel.expr_type == "column":
                if resolve(sel.table_alias) != from_tname:
                    raise ValueError(
                        "Non-aggregated SELECT columns in a GROUP BY query must "
                        "come from the FROM table in V1."
                    )
                # A bare column that is not a group key is invalid standard SQL
                # (its value within a group is ambiguous; PostgreSQL rejects it,
                # SQLite picks an arbitrary row). Encoding it as the group
                # leader's value would silently invent deterministic semantics.
                if sel.col_name not in group_key_cols:
                    raise ValueError(
                        f"Non-aggregated SELECT column '{sel.col_name}' must "
                        "appear in GROUP BY."
                    )
        if is_right:
            # Null-extended right rows have NULL FROM-side keys and form one
            # extra group. If a key column were nullable, matched rows could
            # also carry NULL keys and would have to merge with that group —
            # not modelled, so reject rather than risk a wrong verdict.
            from_table_obj = schema.get_table(from_tname)
            for (_, gcol) in parsed.group_by:
                col_obj = from_table_obj.get_column(gcol) if from_table_obj else None
                if col_obj is None or col_obj.nullable:
                    raise ValueError(
                        "RIGHT JOIN with GROUP BY requires non-nullable "
                        f"group key columns in V1 ('{gcol}' is nullable)."
                    )

        def group_key_eq(i: int, g: int) -> BoolRef:
            clauses = []
            for (galias, gcol) in parsed.group_by:
                n1, v1 = db.cell(from_tname, gcol, i)
                n2, v2 = db.cell(from_tname, gcol, g)
                # NULLs group together (SQL groups NULL keys into one group).
                clauses.append(Or(And(n1, n2),
                                  And(Not(n1), Not(n2), v1 == v2)))
            return And(clauses) if clauses else BoolVal(True)

        def alive(i: int) -> BoolRef:
            """FROM row i contributes at least one surviving post-WHERE row."""
            if has_join:
                terms = [pair_present(i, j) for j in range(bound)]
                if is_left:
                    terms.append(nullext_left_present(i))
                return Or(terms)
            return single_present(i)

        for g in range(bound):
            # Row g leads its group iff it is alive and no earlier alive row
            # shares its key. "Alive" already implies the group is non-empty.
            earlier = [Not(And(alive(h), group_key_eq(h, g))) for h in range(g)]
            base_present = And(alive(g), *earlier) if earlier else alive(g)

            contribs = []
            for i in range(bound):
                keq = group_key_eq(i, g)
                if has_join:
                    for j in range(bound):
                        contribs.append((And(keq, pair_present(i, j)), cellfn_pair(i, j)))
                    if is_left:
                        contribs.append((And(keq, nullext_left_present(i)), cellfn_nullext_left(i)))
                else:
                    contribs.append((And(keq, single_present(i)), cellfn_single(i)))

            cols = []
            for sel in parsed.select_exprs:
                if sel.expr_type == "column":
                    n, v = db.cell(from_tname, sel.col_name, g)   # group-key value
                    cols.append(SymValue(n, v))
                else:
                    cols.append(agg_value(sel, contribs))
            present = And([base_present, *having_clauses(contribs)]) \
                if parsed.having_conditions else base_present
            output.append(OutputTuple(present, cols))

        if is_right:
            # All null-extended right rows share the single all-NULL FROM-side
            # key, so they form exactly one extra candidate group.
            contribs_r = [(nullext_right_present(j), cellfn_nullext_right(j))
                          for j in range(bound)]
            base_present_r = Or([a for a, _ in contribs_r]) if contribs_r else BoolVal(False)

            cols_r = []
            for sel in parsed.select_exprs:
                if sel.expr_type == "column":
                    cols_r.append(SymValue(BoolVal(True), IntVal(0)))  # NULL key
                else:
                    cols_r.append(agg_value(sel, contribs_r))
            present_r = And([base_present_r, *having_clauses(contribs_r)]) \
                if parsed.having_conditions else base_present_r
            output.append(OutputTuple(present_r, cols_r))

    elif has_agg:
        # ── Aggregate without GROUP BY: exactly one output row. ──────────────
        for sel in parsed.select_exprs:
            if sel.expr_type == "column":
                raise ValueError(
                    f"Bare column '{sel.alias}' mixed with aggregates "
                    "requires GROUP BY."
                )

        contribs = []
        for i in range(bound):
            if has_join:
                for j in range(bound):
                    contribs.append((pair_present(i, j), cellfn_pair(i, j)))
                if is_left:
                    contribs.append((nullext_left_present(i), cellfn_nullext_left(i)))
            else:
                contribs.append((single_present(i), cellfn_single(i)))
        if is_right:
            for j in range(bound):
                contribs.append((nullext_right_present(j), cellfn_nullext_right(j)))

        cols = [agg_value(sel, contribs) for sel in parsed.select_exprs]
        # An ungrouped aggregate always returns one row; HAVING may drop it.
        present = And(having_clauses(contribs)) \
            if parsed.having_conditions else BoolVal(True)
        output.append(OutputTuple(present, cols))

    else:
        # ── Plain projection: one tuple per surviving row / matching pair. ───
        if parsed.having_conditions:
            raise ValueError("HAVING without GROUP BY or aggregates is not supported.")
        for i in range(bound):
            if not has_join:
                cols = [proj(sel, cellfn_single(i)) for sel in parsed.select_exprs]
                output.append(OutputTuple(single_present(i), cols))
                continue
            for j in range(bound):
                cols = [proj(sel, cellfn_pair(i, j)) for sel in parsed.select_exprs]
                output.append(OutputTuple(pair_present(i, j), cols))
            if is_left:
                cols = [proj(sel, cellfn_nullext_left(i)) for sel in parsed.select_exprs]
                output.append(OutputTuple(nullext_left_present(i), cols))
        if is_right:
            for j in range(bound):
                cols = [proj(sel, cellfn_nullext_right(j)) for sel in parsed.select_exprs]
                output.append(OutputTuple(nullext_right_present(j), cols))

    return QueryFormula(
        output=output,
        arity=arity,
        col_aliases=col_aliases,
        extra_constraints=extra_constraints,
        bound=bound,
    )


# ── Condition → Z3 helpers ───────────────────────────────────────────────────

def _cmp(op: str, a, b) -> BoolRef:
    """The Z3 relation for a comparison op ('eq'|'neq'|'gt'|'gte'|'lt'|'lte'),
    stripped of any 'having_' prefix. Used to build both the TRUE and FALSE
    halves of a three-valued predicate."""
    op = op.replace("having_", "")
    if op == "eq":  return a == b
    if op == "neq": return a != b
    if op == "gt":  return a > b
    if op == "gte": return a >= b
    if op == "lt":  return a < b
    if op == "lte": return a <= b
    raise ValueError(f"Unsupported comparison operator '{op}'.")


def _eval_tf(node, leaf_tf) -> tuple[BoolRef, BoolRef]:
    """Evaluate a predicate tree under three-valued (Kleene) logic, returning
    the (is_true, is_false) pair. The predicate is NULL exactly when neither
    holds. `leaf_tf(cond)` supplies the pair for a leaf ParsedCondition.

        AND: (tA ∧ tB, fA ∨ fB)      OR: (tA ∨ tB, fA ∧ fB)      NOT: swap (t, f)

    A WHERE/HAVING filter keeps a tuple iff the returned `is_true` holds (paper
    Fig. 5). The `is_false` half only matters under a NOT, where it becomes the
    new `is_true` — which is why ¬φ over a NULL φ stays NULL (both halves false).
    """
    if isinstance(node, ParsedCondition):
        return leaf_tf(node)
    if node.op == "not":
        t, f = _eval_tf(node.children[0], leaf_tf)
        return (f, t)
    parts = [_eval_tf(ch, leaf_tf) for ch in node.children]
    ts = [p[0] for p in parts]
    fs = [p[1] for p in parts]
    if node.op == "and":
        return (And(ts), Or(fs))
    if node.op == "or":
        return (Or(ts), And(fs))
    raise ValueError(f"Unsupported boolean operator '{node.op}'.")


def _pred_tf(
    cond: ParsedCondition,
    alias_map: dict[str, str],
    db: SymbolicDB,
    schema: SchemaModel,
    cellfn,
) -> tuple[BoolRef, BoolRef]:
    """
    Encode a single leaf predicate under three-valued logic, returning
    (is_true, is_false). A comparison against a NULL operand is NULL (both
    halves false); IS [NOT] NULL are the only two-valued predicates. Cells are
    read through `cellfn(alias, col)` so the same predicate works in matched-
    pair and null-extended row contexts.

    Raises ValueError on anything that cannot be encoded — never drops a
    predicate silently.
    """
    is_null, val = cellfn(cond.table_alias, cond.col)

    # NULL checks are total (never NULL themselves): TRUE and FALSE partition.
    if cond.op == "is_null":
        return (is_null, Not(is_null))
    if cond.op == "is_not_null":
        return (Not(is_null), is_null)

    # ── Column-vs-column predicate (e.g. WHERE b = a) ────────────────────────
    # NULL unless both cells are non-NULL; then TRUE/FALSE by the relation.
    if cond.rhs_col is not None:
        r_is_null, r_val = cellfn(cond.rhs_table_alias, cond.rhs_col)
        both_nn = And(Not(is_null), Not(r_is_null))
        rel = _cmp(cond.op, val, r_val)
        return (And(both_nn, rel), And(both_nn, Not(rel)))

    if cond.value is None:
        raise ValueError(f"Unsupported predicate on column '{cond.col}'.")

    tname = alias_map[cond.table_alias]
    table_obj = schema.get_table(tname)
    col_obj = table_obj.get_column(cond.col) if table_obj else None
    col_type = col_obj.col_type if col_obj else "INTEGER"

    if isinstance(cond.value, str):
        if col_type not in ("TEXT", "TIMESTAMP"):
            raise ValueError(
                f"String literal compared to non-text column '{cond.col}' "
                f"({col_type}) is not supported."
            )
        if cond.op not in ("eq", "neq"):
            raise ValueError(
                f"Ordering comparison on TEXT/TIMESTAMP column '{cond.col}' is "
                "not supported in V1 — string/timestamp values are encoded "
                "symbolically (equality only)."
            )
        rhs = IntVal(db.intern_string(cond.value))
    else:
        if col_type in ("TEXT", "TIMESTAMP"):
            raise ValueError(
                f"Numeric literal compared to TEXT/TIMESTAMP column "
                f"'{cond.col}' is not supported."
            )
        db.note_numeric_literal(cond.value)
        rhs = RealVal(float(cond.value)) if col_type == "REAL" else IntVal(int(cond.value))

    # A comparison with a NULL operand is NULL (neither TRUE nor FALSE).
    notnull = Not(is_null)
    rel = _cmp(cond.op, val, rhs)
    return (And(notnull, rel), And(notnull, Not(rel)))


def _having_tf(cond: ParsedCondition, sv: SymValue) -> tuple[BoolRef, BoolRef]:
    """Three-valued (is_true, is_false) for a HAVING aggregate comparison over a
    group's aggregate SymValue. NULL when the aggregate is NULL (e.g. SUM over an
    empty/all-NULL group), so the group survives only when `is_true` holds."""
    val = cond.value
    if val is None:
        raise ValueError("Unsupported HAVING condition.")
    rhs = RealVal(float(val)) if isinstance(val, float) else IntVal(int(val))
    notnull = Not(sv.is_null)
    rel = _cmp(cond.op, sv.value, rhs)
    return (And(notnull, rel), And(notnull, Not(rel)))
