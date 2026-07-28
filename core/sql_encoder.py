"""
core/sql_encoder.py

Parses a SELECT query and encodes it as Z3 formulas over a symbolic database.

Supported subset:
  SELECT   — column refs, SUM(col), COUNT(*), COUNT(col), COALESCE(agg, default)
  FROM     — single table with alias
  JOIN     — any left-deep chain mixing INNER/LEFT/RIGHT/FULL joins, each with
             a simple ON equality condition. Encoded as a fold (see
             build_join_bag): each join's ON may only reference the table
             being joined or a table already in scope (forward references are
             rejected — real SQL never has them either).
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
  CROSS joins, self-joins, a CTE relation on an outer-join side,
  BETWEEN / LIKE predicates, window
  functions, subqueries outside WHERE IN, UNION, DISTINCT, LIMIT/OFFSET,
  string/timestamp ordering comparisons.
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

import math
from dataclasses import dataclass, field
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
    # Membership predicate `col IN (SELECT ...)` (op == 'in_subquery'), the
    # paper's E⃗ ∈ Q (Fig. 4). `subquery` is the parsed body (a ParsedQuery);
    # encode_query materialises it once into `subquery_qf` (a QueryFormula) and
    # _pred_tf turns membership into a guarded three-valued disjunction over its
    # output tuples. Only uncorrelated, single-column bodies in WHERE are allowed.
    subquery: Optional[object] = None
    subquery_qf: Optional[object] = None


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
    join_type: str                # 'INNER' | 'LEFT' | 'RIGHT' | 'FULL'
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
    # Non-recursive CTEs as (name, parsed body), in declaration order. Encoded to
    # relations and materialised into the SymbolicDB by encode_query (VeriEQL's
    # With(Q̃,R⃗,Q)); empty for a query with no WITH clause.
    ctes: list = field(default_factory=list)


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
        # Materialised CTE relations (VeriEQL's With): column types per pseudo-
        # table, keyed by the mangled registration name. These live in `vars` /
        # `nulls` / `exists` like base tables so every accessor works unchanged,
        # but are NOT in `schema.tables`, so domain_constraints() never adds
        # integrity constraints for them (a derived relation has none).
        self.cte_col_types: dict[str, dict[str, str]] = {}
        self._cte_seq: int = 0
        self._build()

    def register_cte_relation(self, name: str, qf: "QueryFormula") -> str:
        """Bind a materialised CTE (a QueryFormula) as a pseudo-table the main
        query can read like a base table, and return its unique registration
        name. Each of the CTE's output tuples becomes one row: the tuple's
        `present` flag is the row's existence, and its (is_null, value) cells are
        expressions over the shared base-table vars — so joining a CTE relation
        reads its cells as values, exactly the paper's D′ = D[Rᵢ ↦ [[Qᵢ]]_D].

        The name is mangled and per-registration unique so a CTE never clobbers a
        base table (or a CTE of the other query being compared over the same db).
        """
        self._cte_seq += 1
        reg = f"__cte_{self._cte_seq}__{name}"
        n = len(qf.output)
        self.vars[reg] = {}
        self.nulls[reg] = {}
        for j, alias in enumerate(qf.col_aliases):
            self.vars[reg][alias] = [qf.output[i].cols[j].value for i in range(n)]
            self.nulls[reg][alias] = [qf.output[i].cols[j].is_null for i in range(n)]
        self.exists[reg] = [qf.output[i].present for i in range(n)]
        self.cte_col_types[reg] = dict(qf.col_types)
        return reg

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
    # Output column types by alias — populated so a materialised CTE relation
    # can answer col_type queries for the query that reads from it.
    col_types: dict[str, str] = field(default_factory=dict)


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
    `IN (SELECT ...)` (the paper's E⃗ ∈ Q semi-join) is a single 'in_subquery'
    leaf, materialised and encoded as a membership disjunction (WHERE only).

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

    # IS [NOT] NULL. sqlglot's AST shape for the NOT form is version-dependent
    # and changed in a *minor* release: up to 30.12 it wrapped the node as
    # Not(Is(...)) (caught by the exp.Not branch above), from 30.13 it keeps a
    # single Is node carrying negate=True. Both shapes must be read here.
    # Reading `negate` is not optional politeness: a version that reports the
    # negation via the flag while we only looked for the wrapper silently
    # encoded `IS NOT NULL` as `IS NULL` — an inverted predicate, which is the
    # false-'equivalent' failure mode the fail-closed rule exists to prevent.
    if isinstance(node, exp.Is):
        col_node = node.this
        if isinstance(col_node, exp.Column) and isinstance(node.expression, exp.Null):
            tbl = col_node.args.get("table")
            return ParsedCondition(
                op="is_not_null" if node.args.get("negate") else "is_null",
                table_alias=tbl.name if tbl else None,
                col=col_node.name,
                value=None,
            )
        raise ValueError(f"Unsupported IS predicate: {node.sql()}")

    raise ValueError(
        f"Unsupported WHERE/HAVING construct: {node.sql()} — supported: "
        "AND / OR / NOT, comparisons, IN (value-list), IN (SELECT ...) in "
        "WHERE, IS [NOT] NULL, and aggregate comparisons."
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
    """Parse an IN predicate into a three-valued leaf/subtree.

    Two forms:
      * `col IN (v1, v2, ...)` — desugared into `col = v1 OR col = v2 OR ...`;
        each item may be a literal or another column (col-vs-col equality leaf).
      * `col IN (SELECT c FROM ...)` — the paper's E⃗ ∈ Q membership (Fig. 4).
        Returns a single `op='in_subquery'` leaf carrying the parsed, uncorrelated
        body; encode_query materialises it and _pred_tf builds the membership
        disjunction. Only a single-column LHS over an uncorrelated, single-column
        body is accepted — everything else fails closed.
    """
    query = node.args.get("query")
    if query is not None:
        return _parse_in_subquery(node, query)

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


def _parse_in_subquery(node: exp.In, query: exp.Expression) -> ParsedCondition:
    """Parse `col IN (SELECT c FROM ...)` into an 'in_subquery' leaf.

    Fail-closed on everything outside the supported subset (uncorrelated,
    single-column LHS, single-column body): a tuple LHS, a multi-column body, or
    a correlated body (an outer-alias reference inside the body — caught later by
    _resolve_references when the body is encoded) must abort, never be dropped.
    """
    left = node.this
    if not isinstance(left, exp.Column):
        raise ValueError(
            "IN (SELECT ...) requires a single-column left-hand side; tuple "
            f"membership is not supported: {node.sql()}")

    body = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(body, exp.Select):
        raise ValueError(f"IN (...) subquery must be a SELECT: {node.sql()}")

    parsed_body = _parse_select_ast(body)
    if len(parsed_body.select_exprs) != 1:
        raise ValueError(
            "IN (SELECT ...) subquery must return exactly one column.")

    tbl = left.args.get("table")
    return ParsedCondition(
        op="in_subquery",
        table_alias=tbl.name if tbl else None,
        col=left.name,
        value=None,
        subquery=parsed_body,
    )


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


# ── SQL → ParsedQuery ────────────────────────────────────────────────────────

def _sub_arg(node: exp.Expression, name: str):
    """Fetch a sqlglot arg tolerant of the 30.x reserved-word rename
    ('from' → 'from_', 'with' → 'with_')."""
    return node.args.get(name) or node.args.get(name + "_")


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

    return _parse_select_ast(stmt)


def _parse_select_ast(stmt: exp.Select) -> ParsedQuery:
    """Parse a SELECT AST (possibly with a WITH clause) into a ParsedQuery.

    Non-recursive CTEs are captured as `ctes` — encode_query materialises each
    one into the SymbolicDB (VeriEQL's With(Q̃,R⃗,Q)) and lets the main query read
    it like a base table. The body is parsed with the WITH stripped, so the rest
    of the subset checks (no subqueries, etc.) apply to the main query alone.
    """
    # ── Non-recursive CTEs → parsed bodies ───────────────────────────────────
    ctes: list = []
    with_node = _sub_arg(stmt, "with")
    if with_node is not None:
        if with_node.args.get("recursive"):
            raise ValueError("Recursive CTEs (WITH RECURSIVE) are not supported.")
        for cte in with_node.expressions:
            name = cte.alias
            if not name:
                raise ValueError("Unnamed CTE is not supported.")
            body = cte.this
            if not isinstance(body, exp.Select):
                raise ValueError(f"CTE '{name}' body must be a SELECT.")
            if _sub_arg(body, "with"):
                raise ValueError(
                    f"A WITH clause nested inside CTE '{name}' is not supported.")
            ctes.append((name, _parse_select_ast(body)))
        # A CTE body referencing another CTE (CTE-on-CTE) is not supported yet.
        cte_names = {nm for nm, _ in ctes}
        for nm, body_pq in ctes:
            refs = {body_pq.from_table} | {j.table_name for j in body_pq.joins}
            if refs & cte_names:
                raise ValueError(
                    "A CTE whose body references another CTE is not supported yet.")
        stmt = stmt.copy()
        stmt.args.pop("with", None)
        stmt.args.pop("with_", None)

    def arg(name: str):
        # sqlglot 30.x renamed reserved-word arg keys ('from' → 'from_', etc.)
        return stmt.args.get(name) or stmt.args.get(name + "_")

    # ── Whole-query constructs outside the V1 subset ─────────────────────────
    if arg("distinct"):
        raise ValueError("SELECT DISTINCT is not supported in V1.")
    if arg("limit"):
        raise ValueError("LIMIT is not supported in V1.")
    if arg("offset"):
        raise ValueError("OFFSET is not supported in V1.")
    if stmt.find(exp.Window):
        raise ValueError("Window functions are not supported in V1.")
    # Subqueries: only `col IN (SELECT ...)` in WHERE is allowed. Collect those
    # subquery SELECTs (and everything nested within them — each validated by the
    # recursive _parse_select_ast in _parse_in_subquery) so the blanket rejection
    # below doesn't fire on them; every other nested SELECT (scalar subquery,
    # EXISTS, derived table in FROM, HAVING subquery) is still rejected here.
    allowed_subquery_selects: set[int] = set()
    _where = arg("where")
    if _where is not None:
        for in_node in _where.find_all(exp.In):
            q = in_node.args.get("query")
            if q is None:
                continue
            inner = q.this if isinstance(q, exp.Subquery) else q
            if isinstance(inner, exp.Select):
                for s in inner.find_all(exp.Select):
                    allowed_subquery_selects.add(id(s))
    if any(s is not stmt and id(s) not in allowed_subquery_selects
           for s in stmt.find_all(exp.Select)):
        raise ValueError(
            "Subqueries are not supported in V1 except `col IN (SELECT ...)` "
            "in WHERE (scalar subqueries, EXISTS, and derived tables are not "
            "supported)."
        )
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

    # ── JOINs (any left-deep chain of INNER/LEFT/RIGHT/FULL) ─────────────────
    # Use the direct `joins` arg, NOT find_all(exp.Join): find_all descends into
    # a WHERE `IN (SELECT ... JOIN ...)` body and would attribute the subquery's
    # joins to this query. The subquery body's own joins are read when it is
    # parsed recursively.
    joins: list[ParsedJoin] = []
    for join_node in (stmt.args.get("joins") or []):
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
            join_type = "FULL"   # FULL [OUTER] JOIN — both sides null-extended
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

    # Join order here is declaration order — encode_query's build_join_bag folds
    # them left-deep in this same order (SQL's default join-evaluation order).

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
        ctes=ctes,
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

    JOIN semantics: FROM + JOINs are folded left-deep into a single bag of
    (present, cellfn) entries (build_join_bag) — VeriEQL's Fig. 5 binary join
    operator generalized to an N-table chain. The ON condition and the WHERE
    clause are kept strictly separate throughout the fold. For outer joins the
    distinction is load-bearing — a filter in WHERE eliminates null-extended
    rows (making LEFT JOIN behave like INNER), while the same filter in ON
    merely restricts which rows match. WHERE is applied once, to the final
    folded tuple, matching the paper's separate σ_φ outer filter.

    Args:
        parsed:  Output of parse_query().
        db:      Shared SymbolicDB — must be the SAME instance for both queries
                 being compared in equivalence.py.
        schema:  The SchemaModel (used for alias → table name resolution).
    """
    bound = db.bound

    def col_type_of(tname: str, col: str) -> str:
        """Column type for a base table (via schema) or a materialised CTE
        relation (via db.cte_col_types). Defaults to INTEGER when unknown."""
        tobj = schema.get_table(tname)
        if tobj is not None:
            c = tobj.get_column(col)
            if c is not None:
                return c.col_type
        return db.cte_col_types.get(tname, {}).get(col, "INTEGER")

    # ── Materialise CTEs (VeriEQL With(Q̃,R⃗,Q), Fig. 5) ───────────────────────
    # Encode each CTE body to a QueryFormula and bind it into the SymbolicDB as a
    # pseudo-table (D′ = D[Rᵢ ↦ [[Qᵢ]]_D]). The main query then reads a CTE
    # relation exactly like a base table. Encoding order = declaration order.
    cte_regs: dict[str, str] = {}
    cte_constraints: list = []
    for cte_name, cte_parsed in parsed.ctes:
        cte_qf = encode_query(cte_parsed, db, schema)
        if len(set(cte_qf.col_aliases)) != len(cte_qf.col_aliases):
            raise ValueError(
                f"CTE '{cte_name}' has duplicate output column names; alias "
                "them uniquely so the reading query can reference each.")
        cte_constraints.extend(cte_qf.extra_constraints)
        cte_regs[cte_name] = db.register_cte_relation(cte_name, cte_qf)

    # ── Materialise WHERE `col IN (SELECT ...)` bodies (E⃗ ∈ Q, Fig. 4) ────────
    # Encode each subquery body once (not per bag row) against the shared db, so
    # _pred_tf can build the membership disjunction over its output tuples. The
    # body is encoded via plain encode_query, so it cannot see this query's
    # cte_regs — a subquery reading an outer CTE name fails closed ("not found").
    # A correlated body (outer-alias reference) fails closed inside its own
    # _resolve_references. Result columns are NOT registered as a pseudo-table:
    # membership reads the tuples directly, and the witness builder never sees them.
    for cond in _iter_pred_leaves(parsed.where_conditions):
        if cond.subquery is not None:
            sq = encode_query(cond.subquery, db, schema)
            cte_constraints.extend(sq.extra_constraints)
            cond.subquery_qf = sq

    def map_name(tname: str) -> str:
        # A FROM/JOIN name that is a CTE resolves to its registration name.
        return cte_regs.get(tname, tname)

    # ── Alias → table/relation name resolution & fail-closed validation ──────
    alias_map: dict[str, str] = {parsed.from_alias: map_name(parsed.from_table)}
    for join in parsed.joins:
        alias_map[join.alias] = map_name(join.table_name)

    for tname in alias_map.values():
        if tname not in db.vars:
            raise ValueError(f"Table '{tname}' not found in the schema.")

    _resolve_references(parsed, alias_map, db)

    def resolve(alias: str) -> str:
        return alias_map[alias]

    from_alias = parsed.from_alias
    from_tname = map_name(parsed.from_table)

    # Self-join guard: two aliases of the same table would unsoundly share the
    # same symbolic rows. (CTE relations get distinct registration names, so a
    # CTE over table T joined with T is fine — they are independent relations.)
    if len(set(alias_map.values())) != len(alias_map):
        raise ValueError("Self-joins are not supported.")

    # ── Outer-join CTE guard ──────────────────────────────────────────────────
    # A materialised CTE relation is usable only in FROM / INNER-join positions.
    # On an outer-join side its cells would need null-extension handling this
    # encoder doesn't provide for a relation — fail-closed rather than risk a
    # false "equivalent". (Any outer join anywhere in the chain trips this,
    # even if the CTE alias itself is only INNER-joined — lifting that to
    # "only the outer-joined alias matters" is a separate soundness argument,
    # left for a future pass.)
    outer_joins = [j for j in parsed.joins if j.join_type in ("LEFT", "RIGHT", "FULL")]
    if outer_joins and any(v in db.cte_col_types for v in alias_map.values()):
        raise ValueError(
            "A CTE relation combined with an outer (LEFT/RIGHT/FULL) join is not "
            "supported yet — use a CTE in FROM or INNER-join positions.")

    # ── WHERE: evaluated on the post-join tuple (three-valued: TRUE keeps) ───
    # Paper Fig. 5: σ_φ(Q) = filter(Q, λx.[[φ]]_x = ⊤) — a row survives iff the
    # predicate's `is_true` half holds (NULL and FALSE both drop it).
    def type_of(alias: str, col: str) -> str:
        return col_type_of(resolve(alias), col)

    def where_all(cellfn) -> BoolRef:
        if parsed.where_conditions is None:
            return BoolVal(True)
        t, _ = _eval_tf(
            parsed.where_conditions,
            lambda cond: _pred_tf(cond, db, type_of, cellfn),
        )
        return t

    # ── Join bag: left-deep fold over FROM + JOINs ────────────────────────────
    # VeriEQL's Fig. 5 join operators are binary (Q1 ⊗ Q2), so an N-table chain
    # is left-deep nesting (Q1 ⊗ Q2) ⊗ Q3 …. This fold mirrors that structure
    # directly: each step joins one more table onto the accumulated relation.
    def build_join_bag() -> list[tuple[BoolRef, object]]:
        """Materialise the FROM+JOIN result as a bag of (present, cellfn)
        entries, folding parsed.joins left-deep in declaration order.

        Each step: INNER keeps only matched combinations; LEFT/FULL also emit
        a null-extended entry for every accumulated row with no match; RIGHT/
        FULL also emit one for every new-table row with no match, NULL-
        extending EVERY alias accumulated so far — the paper's RIGHT/FULL
        null-extends against the whole accumulated left relation, not just the
        immediately preceding table, so a later RIGHT/FULL in a chain nulls
        every earlier alias in that output row.

        WHERE is applied once, to the final folded tuple, never during the
        fold — it is a separate outer filter (σ_φ) from a join's own ON (φ on
        the join operator), and folding it in early (the pre-chain behaviour)
        got ON-vs-WHERE wrong for outer joins.

        cellfn(alias, col) -> (is_null, value); a null-extended alias reads as
        (True, 0). Size is bound^n over n INNER-joined tables, +O(bound) per
        LEFT/RIGHT/FULL level.
        """
        def cellfn_from_idx(idx: dict):
            def f(alias, col):
                i = idx.get(alias)
                if i is None:
                    return (BoolVal(True), IntVal(0))
                return db.cell(resolve(alias), col, i)
            return f

        covered = [from_alias]
        bag: list[tuple[BoolRef, dict]] = [
            (db.exists[from_tname][i], {from_alias: i})
            for i in range(len(db.exists[from_tname]))
        ]

        for jn in parsed.joins:
            a = jn.alias
            tname = resolve(a)
            nrows = len(db.exists[tname])
            left_ext = jn.join_type in ("LEFT", "FULL")
            right_ext = jn.join_type in ("RIGHT", "FULL")

            # ON scope: each side must be the table just joined or one already
            # in scope. Forward references (an ON referencing a table joined
            # later in the chain) aren't valid SQL either — the FROM clause
            # introduces tables left to right — so this narrows nothing real.
            allowed = set(covered) | {a}
            if jn.on_left_alias not in allowed or jn.on_right_alias not in allowed:
                raise ValueError(
                    f"JOIN ON for '{a}' references a table not yet in scope; "
                    "a join's ON condition may only reference the table being "
                    "joined or a table introduced earlier in the FROM clause."
                )
            if a not in (jn.on_left_alias, jn.on_right_alias):
                raise ValueError(
                    f"JOIN ON for '{a}' must reference the joined table.")

            def on3v(idx: dict, j: int, _jn=jn, _a=a) -> BoolRef:
                cf = cellfn_from_idx({**idx, _a: j})
                ln, lv = cf(_jn.on_left_alias, _jn.on_left_col)
                rn, rv = cf(_jn.on_right_alias, _jn.on_right_col)
                return And(Not(ln), Not(rn), lv == rv)

            new_bag: list[tuple[BoolRef, dict]] = []
            # matched_row[j] collects (entry_present ∧ matched) across every
            # accumulated entry, for RIGHT/FULL's "no accumulated row matched"
            # check below.
            matched_row: list[list[BoolRef]] = [[] for _ in range(nrows)]

            for e_present, e_idx in bag:
                row_matches = []
                for j in range(nrows):
                    m = And(db.exists[tname][j], on3v(e_idx, j))
                    row_matches.append(m)
                    matched_row[j].append(And(e_present, m))
                    new_idx = dict(e_idx)
                    new_idx[a] = j
                    new_bag.append((And(e_present, m), new_idx))
                if left_ext:
                    ext_idx = dict(e_idx)
                    ext_idx[a] = None
                    any_match = Or(row_matches) if row_matches else BoolVal(False)
                    new_bag.append((And(e_present, Not(any_match)), ext_idx))

            if right_ext:
                for j in range(nrows):
                    any_match = Or(matched_row[j]) if matched_row[j] else BoolVal(False)
                    ext_idx = {alias: None for alias in covered}
                    ext_idx[a] = j
                    new_bag.append((And(db.exists[tname][j], Not(any_match)), ext_idx))

            bag = new_bag
            covered.append(a)

        return [(And(present, where_all(cellfn_from_idx(idx))), cellfn_from_idx(idx))
                for present, idx in bag]

    # ── Projection of one SELECT column through a cell accessor ──────────────
    def proj(sel: ParsedSelectExpr, cellfn) -> SymValue:
        n, v = cellfn(sel.table_alias, sel.col_name)
        return SymValue(n, v)

    # ── Aggregate over a list of contributions (active_bool, cell_fn) ────────
    def agg_value(sel: ParsedSelectExpr, contribs: list) -> SymValue:
        is_real = (sel.col_name is not None
                   and col_type_of(resolve(sel.table_alias), sel.col_name) == "REAL")
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
    extra_constraints: list = list(cte_constraints)
    output: list[OutputTuple] = []

    # Output column types (so this query, if used as a CTE, can answer col_type
    # queries for whatever reads it): COUNT → INTEGER, SUM → its column's numeric
    # type, a projected column → its source type.
    result_col_types: dict[str, str] = {}
    for sel in parsed.select_exprs:
        if sel.expr_type == "column":
            result_col_types[sel.alias] = col_type_of(resolve(sel.table_alias), sel.col_name)
        elif sel.expr_type == "sum":
            src = col_type_of(resolve(sel.table_alias), sel.col_name) if sel.col_name else "INTEGER"
            result_col_types[sel.alias] = "REAL" if src == "REAL" else "INTEGER"
        else:  # count_star / count_col
            result_col_types[sel.alias] = "INTEGER"

    # ── Build the join bag, then project/aggregate/group over it ─────────────
    bag = build_join_bag()

    if has_group:
        # GROUP BY keys (and bare SELECT columns) may come from ANY joined
        # table (incl. a null-extended outer side) — dedup runs over join-bag
        # rows, and group_key_eq is NULL-safe (NULL keys group together), so a
        # nullable key from a RIGHT/FULL null-extension merges correctly.
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
        col_types=result_col_types,
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
    db: SymbolicDB,
    type_of,
    cellfn,
) -> tuple[BoolRef, BoolRef]:
    """
    Encode a single leaf predicate under three-valued logic, returning
    (is_true, is_false). A comparison against a NULL operand is NULL (both
    halves false); IS [NOT] NULL are the only two-valued predicates. Cells are
    read through `cellfn(alias, col)` so the same predicate works in matched-
    pair and null-extended row contexts; `type_of(alias, col)` gives the column
    type (base table or materialised CTE relation).

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

    # ── Membership: col IN (SELECT c ...) — the paper's E⃗ ∈ Q (Fig. 4) ───────
    # Three-valued semantics over the (once-materialised) subquery output bag:
    #   is_true  = ∨_i (present_i ∧ ¬xn ∧ ¬rn_i ∧ xv = rv_i)
    #   is_false = ∧_i (¬present_i ∨ (¬xn ∧ ¬rn_i ∧ xv ≠ rv_i))
    # This yields NULL (neither half) when xv is NULL against a non-empty body,
    # and gets the NOT IN NULL trap right for free: a NULL body cell blocks
    # is_false, so ¬(IN) (the NOT swap in _eval_tf) can never become TRUE. An
    # empty body → is_false is vacuously TRUE, so IN is FALSE / NOT IN is TRUE.
    if cond.op == "in_subquery":
        qf = cond.subquery_qf
        if qf is None:
            raise ValueError("IN (SELECT ...) body was not materialised.")
        lhs_type = type_of(cond.table_alias, cond.col)
        body_type = qf.col_types.get(qf.col_aliases[0], "INTEGER")
        # TEXT/TIMESTAMP are globally interned to opaque ints; equality is only
        # sound against another interned string column, never a plain number.
        if (lhs_type in ("TEXT", "TIMESTAMP")) != (body_type in ("TEXT", "TIMESTAMP")):
            raise ValueError(
                f"IN (SELECT ...) compares column '{cond.col}' ({lhs_type}) to a "
                f"subquery column of type {body_type}; the types are incompatible."
            )
        ts, fs = [], []
        for tup in qf.output:
            rn, rv = tup.cols[0].is_null, tup.cols[0].value
            ts.append(And(tup.present, Not(is_null), Not(rn), val == rv))
            fs.append(Or(Not(tup.present), And(Not(is_null), Not(rn), val != rv)))
        return (Or(ts) if ts else BoolVal(False),
                And(fs) if fs else BoolVal(True))

    if cond.value is None:
        raise ValueError(f"Unsupported predicate on column '{cond.col}'.")

    col_type = type_of(cond.table_alias, cond.col)

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
