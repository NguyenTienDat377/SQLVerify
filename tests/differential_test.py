"""
tests/differential_test.py

Differential testing of SQLVerify's Z3 encoding against concrete SQLite
execution (the missing test layer named in CLAUDE.md).

For each seed:
  1. Generate a random schema and a random query pair within the V1 subset
     (base query + one mutation, or an identical pair).
  2. Ask check_equivalence() for a verdict at the given bound.
  3. Cross-examine the verdict against ground truth:
       - 'equivalent'  → sample many concrete databases (each ≤ bound rows per
         table, values inside the encoder's finite window, PK/FK/NOT NULL
         respected) and run both queries on SQLite. Any database on which the
         output bags differ is a FALSE-EQUIVALENT — the one failure mode the
         verifier must not have. Hard failure.
       - 'divergent'   → independently replay the counterexample database on
         SQLite and assert the output bags really differ.
       - 'error'       → the generator only emits supported syntax, so any
         error is a bug (in the generator or the encoder). Hard failure.
       - 'unknown'     → tallied, not a failure (solver timeout).
  4. Identical pairs must verify as 'equivalent'.

Verdicts are *bounded* claims (databases with at most `bound` rows per
relation, Def. 3.5 of the VeriEQL paper), so the sampler never exceeds that
bound — the oracle checks exactly what the verdict asserts.

On any failure the seed, DDL, queries, and distinguishing database are
printed, so a run is reproducible with --seed-start.

Run:
    .venv/bin/python tests/differential_test.py                 # 200 seeds, bound 2
    .venv/bin/python tests/differential_test.py --seeds 50 --bound 3
"""

from __future__ import annotations

import argparse
import copy
import os
import random
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.equivalence import check_equivalence

TEXT_POOL = ["red", "blue", "green"]
INT_LO, INT_HI = -2, 6        # sampled cell values; always inside the
LIT_LO, LIT_HI = -2, 5        # encoder's window (span >= 4*bound >= 8)


# ── Schema generation ────────────────────────────────────────────────────────

@dataclass
class GenColumn:
    name: str
    ctype: str                      # 'INTEGER' | 'TEXT'
    nullable: bool = True
    pk: bool = False
    fk: Optional[tuple[str, str]] = None   # (parent_table, parent_col)


@dataclass
class GenTable:
    name: str
    columns: list[GenColumn] = field(default_factory=list)

    def col(self, name: str) -> GenColumn:
        return next(c for c in self.columns if c.name == name)


def gen_schema(rng: random.Random) -> list[GenTable]:
    """Three-table schema with randomized nullability, FK, and TEXT column.

    t3 chains off t2 (t3.t2_id -> t2.id) so the generator can emit 3-table
    INNER-join chains (t1 x JOIN t2 y JOIN t3 z). Tables are ordered so the
    concrete sampler resolves each FK against an already-populated parent.
    """
    t1 = GenTable("t1", [
        GenColumn("id", "INTEGER", nullable=False, pk=True),
        GenColumn("a", "INTEGER", nullable=rng.random() < 0.7),
        GenColumn("b", "INTEGER", nullable=rng.random() < 0.7),
    ])
    if rng.random() < 0.5:
        t1.columns.append(GenColumn("s", "TEXT", nullable=True))
    t2 = GenTable("t2", [
        GenColumn("id", "INTEGER", nullable=False, pk=True),
        GenColumn("t1_id", "INTEGER", nullable=True,
                  fk=("t1", "id") if rng.random() < 0.5 else None),
        GenColumn("c", "INTEGER", nullable=rng.random() < 0.7),
    ])
    t3 = GenTable("t3", [
        GenColumn("id", "INTEGER", nullable=False, pk=True),
        GenColumn("t2_id", "INTEGER", nullable=True,
                  fk=("t2", "id") if rng.random() < 0.5 else None),
        GenColumn("e", "INTEGER", nullable=rng.random() < 0.7),
    ])
    return [t1, t2, t3]


def render_ddl(tables: list[GenTable]) -> str:
    stmts = []
    for t in tables:
        defs = []
        for c in t.columns:
            d = f"{c.name} {c.ctype}"
            if c.pk:
                d += " PRIMARY KEY"
            elif not c.nullable:
                d += " NOT NULL"
            if c.fk:
                d += f" REFERENCES {c.fk[0]}({c.fk[1]})"
            defs.append(d)
        stmts.append(f"CREATE TABLE {t.name} (\n    " + ",\n    ".join(defs) + "\n);")
    return "\n".join(stmts)


# ── Query generation (V1 subset only) ────────────────────────────────────────

@dataclass
class Pred:
    kind: str                       # 'cmp_lit' | 'cmp_col' | 'null' | 'in' | 'or' | 'not'
    lhs: Optional[tuple[str, str]] = None   # (alias, col) for atoms / 'in'
    op: str = "="                   # = <> > >= < <=  (or IS [NOT] NULL / IN / NOT IN)
    lit: object = None              # int or str for cmp_lit
    rhs: Optional[tuple[str, str]] = None   # for cmp_col
    lits: Optional[list] = None     # int literal list for 'in' / 'not in'
    children: Optional[list] = None  # sub-Preds for 'or' (≥2) / 'not' (exactly 1)

    def render(self) -> str:
        if self.kind == "or":
            return "(" + " OR ".join(c.render() for c in self.children) + ")"
        if self.kind == "not":
            return "NOT (" + self.children[0].render() + ")"
        if self.kind == "in":
            vals = ", ".join(str(v) for v in self.lits)
            return f"{self.lhs[0]}.{self.lhs[1]} {self.op} ({vals})"
        l = f"{self.lhs[0]}.{self.lhs[1]}"
        if self.kind == "null":
            return f"{l} {self.op}"          # op is 'IS NULL' / 'IS NOT NULL'
        if self.kind == "cmp_col":
            return f"{l} {self.op} {self.rhs[0]}.{self.rhs[1]}"
        lit = f"'{self.lit}'" if isinstance(self.lit, str) else str(self.lit)
        return f"{l} {self.op} {lit}"


@dataclass
class SelectItem:
    kind: str                       # 'col' | 'count_star' | 'count_col' | 'sum'
    src: Optional[tuple[str, str]] = None    # (alias, col) for col/count_col/sum
    coalesce_zero: bool = False              # wrap SUM in COALESCE(.., 0)
    alias: str = "o"

    def expr_only(self) -> str:
        """The select expression without any ` AS alias` (for CTE re-aliasing)."""
        if self.kind == "col":
            return f"{self.src[0]}.{self.src[1]}"
        if self.kind == "count_star":
            return "COUNT(*)"
        if self.kind == "count_col":
            return f"COUNT({self.src[0]}.{self.src[1]})"
        inner = f"SUM({self.src[0]}.{self.src[1]})"
        if self.coalesce_zero:
            inner = f"COALESCE({inner}, 0)"
        return inner

    def render(self) -> str:
        if self.kind == "col":
            return self.expr_only()
        return f"{self.expr_only()} AS {self.alias}"


@dataclass
class Having:
    agg: str                        # 'count_star' | 'sum'
    col: Optional[tuple[str, str]]  # for sum
    op: str
    lit: int

    def render(self) -> str:
        a = "COUNT(*)" if self.agg == "count_star" else f"SUM({self.col[0]}.{self.col[1]})"
        return f"{a} {self.op} {self.lit}"


@dataclass
class GenQuery:
    join_type: Optional[str]        # None | 'INNER' | 'LEFT' | 'RIGHT'
    select: list[SelectItem]
    where: list[Pred]
    group_by: list[tuple[str, str]]
    having: Optional[Having]

    def render(self) -> str:
        sql = "SELECT " + ", ".join(s.render() for s in self.select)
        sql += " FROM t1 x"
        if self.join_type == "INNER3":
            sql += (" JOIN t2 y ON x.id = y.t1_id"
                    " JOIN t3 z ON y.id = z.t2_id")
        elif self.join_type:
            kw = {"INNER": "INNER JOIN", "LEFT": "LEFT JOIN", "RIGHT": "RIGHT JOIN"}[self.join_type]
            sql += f" {kw} t2 y ON x.id = y.t1_id"
        if self.where:
            sql += " WHERE " + " AND ".join(p.render() for p in self.where)
        if self.group_by:
            sql += " GROUP BY " + ", ".join(f"{a}.{c}" for a, c in self.group_by)
        if self.having:
            sql += " HAVING " + self.having.render()
        return sql


def _query_cols(tables: list[GenTable], join_type: Optional[str]):
    """(alias, col, ctype, nullable) for every column visible to the query."""
    out = []
    for c in tables[0].columns:
        out.append(("x", c.name, c.ctype, c.nullable))
    if join_type is not None:
        for c in tables[1].columns:
            out.append(("y", c.name, c.ctype, c.nullable))
    if join_type == "INNER3":
        for c in tables[2].columns:
            out.append(("z", c.name, c.ctype, c.nullable))
    return out


def gen_atom(rng: random.Random, cols) -> Pred:
    """A single atomic predicate: comparison to a literal/column or IS [NOT] NULL."""
    num_cols = [(a, n) for a, n, t, _ in cols if t == "INTEGER"]
    txt_cols = [(a, n) for a, n, t, _ in cols if t == "TEXT"]
    roll = rng.random()
    if roll < 0.5 or (roll < 0.65 and not txt_cols):
        return Pred("cmp_lit", rng.choice(num_cols),
                    rng.choice(["=", "<>", ">", ">=", "<", "<="]),
                    rng.randint(LIT_LO, LIT_HI))
    if roll < 0.65:
        return Pred("cmp_lit", rng.choice(txt_cols),
                    rng.choice(["=", "<>"]), rng.choice(TEXT_POOL))
    if roll < 0.85:
        return Pred("null", rng.choice([(a, n) for a, n, _, _ in cols]),
                    rng.choice(["IS NULL", "IS NOT NULL"]))
    return Pred("cmp_col", rng.choice(num_cols),
                rng.choice(["=", "<>", ">", ">=", "<", "<="]),
                rhs=rng.choice(num_cols))


def gen_pred(rng: random.Random, cols) -> Pred:
    """A WHERE conjunct: usually an atom, but sometimes an OR-group, an
    IN / NOT IN over an integer list, or a NOT-wrapped atom — so the fuzzer
    attacks the three-valued OR/IN/NOT encoding against the SQLite oracle."""
    num_cols = [(a, n) for a, n, t, _ in cols if t == "INTEGER"]
    roll = rng.random()
    if roll < 0.14 and num_cols:
        # col IN (v1, v2[, v3]) / NOT IN — integer literals only (a NULL in the
        # list is rejected fail-closed by the encoder, so we never generate one).
        lits = [rng.randint(LIT_LO, LIT_HI)
                for _ in range(rng.randint(2, 3))]
        return Pred("in", rng.choice(num_cols),
                    rng.choice(["IN", "NOT IN"]), lits=lits)
    if roll < 0.28:
        # (atom OR atom) — occasionally a 3-way disjunction.
        k = rng.choice([2, 2, 3])
        return Pred("or", children=[gen_atom(rng, cols) for _ in range(k)])
    if roll < 0.35:
        # NOT (atom) — exercises the ¬φ tree node and its three-valued swap.
        return Pred("not", children=[gen_atom(rng, cols)])
    return gen_atom(rng, cols)


def gen_query(rng: random.Random, tables: list[GenTable]) -> GenQuery:
    join_type = rng.choice([None, None, "INNER", "LEFT", "RIGHT", "INNER3"])
    cols = _query_cols(tables, join_type)
    num_cols = [(a, n) for a, n, t, _ in cols if t == "INTEGER"]

    shape = rng.random()
    group_by: list[tuple[str, str]] = []
    having = None
    if shape < 0.55:
        # plain projection, 1-2 columns
        k = rng.randint(1, 2)
        select = [SelectItem("col", src=rng.choice([(a, n) for a, n, _, _ in cols]))
                  for _ in range(k)]
    elif shape < 0.85:
        # GROUP BY key + one aggregate. INNER modes (incl. no join) may group by
        # any visible column; the single-outer path keeps the V1 restriction of
        # FROM-table keys, non-nullable under RIGHT JOIN.
        if join_type in (None, "INNER", "INNER3"):
            key_pool = [(a, n) for a, n, _, _ in cols]
        else:
            key_pool = [("x", c.name) for c in tables[0].columns
                        if join_type != "RIGHT" or not c.nullable]
        key = rng.choice(key_pool)
        group_by = [key]
        select = [SelectItem("col", src=key), _gen_agg(rng, num_cols)]
        if rng.random() < 0.4:
            if rng.random() < 0.6:
                having = Having("count_star", None, rng.choice([">", ">=", "="]),
                                rng.randint(1, 2))
            else:
                having = Having("sum", rng.choice(num_cols),
                                rng.choice([">", "<", ">="]), rng.randint(LIT_LO, LIT_HI))
    else:
        # ungrouped aggregate(s)
        select = [_gen_agg(rng, num_cols)]
        if rng.random() < 0.3:
            second = _gen_agg(rng, num_cols)
            second.alias = "o2"
            select.append(second)

    where = [gen_pred(rng, cols) for _ in range(rng.choice([0, 0, 1, 1, 2]))]
    return GenQuery(join_type, select, where, group_by, having)


def _gen_agg(rng: random.Random, num_cols) -> SelectItem:
    roll = rng.random()
    if roll < 0.3:
        return SelectItem("count_star")
    if roll < 0.55:
        return SelectItem("count_col", src=rng.choice(num_cols))
    return SelectItem("sum", src=rng.choice(num_cols),
                      coalesce_zero=rng.random() < 0.4)


_FLIP = {">": ">=", ">=": ">", "<": "<=", "<=": "<", "=": "<>", "<>": "="}


def mutate(rng: random.Random, q: GenQuery, tables: list[GenTable]) -> tuple[GenQuery, str]:
    """Return (mutant, mutation_name). 'identity' leaves the query unchanged."""
    m = copy.deepcopy(q)
    cols = _query_cols(tables, m.join_type)
    num_cols = [(a, n) for a, n, t, _ in cols if t == "INTEGER"]

    options = ["identity"]
    if m.where:
        options += ["flip_op", "shift_lit", "drop_pred", "flip_null"]
    options.append("add_pred")
    if m.join_type in ("INNER", "LEFT", "RIGHT"):
        options.append("swap_join")
    if any(s.kind in ("count_star", "count_col") for s in m.select):
        options.append("count_swap")
    if any(s.kind == "sum" for s in m.select):
        options.append("coalesce_swap")

    while True:
        choice = rng.choice(options)
        if choice == "identity":
            return m, choice
        if choice == "flip_op":
            cands = [p for p in m.where if p.kind in ("cmp_lit", "cmp_col")]
            if not cands:
                continue
            p = rng.choice(cands)
            p.op = _FLIP[p.op]
            return m, choice
        if choice == "shift_lit":
            cands = [p for p in m.where if p.kind == "cmp_lit" and isinstance(p.lit, int)]
            if not cands:
                continue
            p = rng.choice(cands)
            p.lit += rng.choice([-1, 1])
            return m, choice
        if choice == "drop_pred":
            m.where.pop(rng.randrange(len(m.where)))
            return m, choice
        if choice == "flip_null":
            cands = [p for p in m.where if p.kind == "null"]
            if not cands:
                continue
            p = rng.choice(cands)
            p.op = "IS NOT NULL" if p.op == "IS NULL" else "IS NULL"
            return m, choice
        if choice == "add_pred":
            m.where.append(gen_pred(rng, cols))
            return m, choice
        if choice == "swap_join":
            new = rng.choice([t for t in ("INNER", "LEFT", "RIGHT")
                              if t != m.join_type])
            if new in ("LEFT", "RIGHT") and m.group_by:
                # The single-outer path keeps the V1 GROUP BY restriction:
                # keys must be FROM-table columns, non-nullable under RIGHT.
                # An INNER query may group by a joined-table column (lifted), so
                # fall back to INNER rather than emit an invalid outer query.
                nullable = {c.name for c in tables[0].columns if c.nullable}
                non_from = any(a != "x" for a, _ in m.group_by)
                bad_right = new == "RIGHT" and any(c in nullable for _, c in m.group_by)
                if non_from or bad_right:
                    new = "INNER"
            m.join_type = new
            return m, choice
        if choice == "count_swap":
            s = rng.choice([s for s in m.select if s.kind in ("count_star", "count_col")])
            if s.kind == "count_star":
                s.kind, s.src = "count_col", rng.choice(num_cols)
            else:
                s.kind, s.src = "count_star", None
            return m, choice
        if choice == "coalesce_swap":
            s = rng.choice([s for s in m.select if s.kind == "sum"])
            s.coalesce_zero = not s.coalesce_zero
            return m, choice


# ── CTE wrapping (exercises the non-recursive CTE inliner vs SQLite) ──────────

def _pred_aliases(p: Pred) -> set:
    """Table aliases a predicate (tree) touches."""
    if p.kind == "cmp_col":
        return {p.lhs[0], p.rhs[0]}
    if p.kind in ("or", "not"):
        out = set()
        for c in p.children:
            out |= _pred_aliases(c)
        return out
    return {p.lhs[0]} if p.lhs else set()


def cte_wrap(q: GenQuery, tables: list[GenTable], rng: random.Random) -> str:
    """Rewrite q into a SEMANTICS-PRESERVING CTE form that wraps the FROM table
    (t1) in `WITH wcte AS (SELECT <t1 cols> FROM t1 [WHERE …]) … FROM wcte`.

    Predicates that touch only t1 are pushed into the CTE — sound iff t1 is not
    null-extended (no outer join), so filtering it early equals filtering late.
    This exercises the inliner's projection-mapping and WHERE-conjoin paths while
    keeping wcte-form ≡ q, so check_seed's pair invariants still hold. SQLite runs
    the CTE natively; our engine flattens it — any flatten bug shows as a bag
    mismatch on the sampled databases."""
    t1 = tables[0]
    proj = ", ".join(f"x.{c.name} AS {c.name}" for c in t1.columns)
    body = f"SELECT {proj} FROM t1 x"

    kept = list(q.where)
    if q.join_type not in ("LEFT", "RIGHT"):
        movable = [p for p in q.where if _pred_aliases(p) <= {"x"}]
        if movable:
            body += " WHERE " + " AND ".join(p.render() for p in movable)
            moved = {id(p) for p in movable}
            kept = [p for p in q.where if id(p) not in moved]

    outer = copy.deepcopy(q)
    outer.where = kept
    inner = outer.render().replace(" FROM t1 x", " FROM wcte x", 1)
    return f"WITH wcte AS ({body}) {inner}"


def materialize_wrap(q: GenQuery) -> str:
    """Wrap the ENTIRE query as a CTE and select it back: `WITH _m AS (q') SELECT
    _m.c0,... FROM _m`, where q' is q with its projections aliased c0..cn. This is
    `SELECT * FROM (q)` ≡ q under bag semantics, so it preserves meaning while
    forcing the engine to MATERIALIZE q — including aggregating, multi-table, and
    outer-join CTE bodies (the cases inlining could never handle). SQLite runs the
    CTE natively; any materialization bug shows as a bag mismatch."""
    body_sel = ", ".join(f"{s.expr_only()} AS c{i}" for i, s in enumerate(q.select))
    full = q.render()
    rest = full[full.index(" FROM "):]          # " FROM t1 x [joins] [where] …"
    body = f"SELECT {body_sel}{rest}"
    outer_cols = ", ".join(f"_m.c{i}" for i in range(len(q.select)))
    return f"WITH _m AS ({body}) SELECT {outer_cols} FROM _m"


# ── Concrete database sampling & SQLite execution ────────────────────────────

def sample_db(rng: random.Random, tables: list[GenTable], max_rows: int) -> dict:
    """Random concrete database: ≤ max_rows per table, PK/FK/NOT NULL hold."""
    data: dict[str, list[dict]] = {}
    for t in tables:
        n = rng.randint(0, max_rows)
        pk_cols = [c for c in t.columns if c.pk]
        pk_vals = rng.sample(range(0, 7), n) if pk_cols else None
        rows = []
        for i in range(n):
            row = {}
            for c in t.columns:
                if c.pk:
                    row[c.name] = pk_vals[i]
                elif c.fk:
                    parents = [r[c.fk[1]] for r in data.get(c.fk[0], [])]
                    if parents and rng.random() > 0.3:
                        row[c.name] = rng.choice(parents)
                    else:
                        row[c.name] = None      # FK columns are nullable here
                elif c.nullable and rng.random() < 0.3:
                    row[c.name] = None
                elif c.ctype == "TEXT":
                    row[c.name] = rng.choice(TEXT_POOL + ["other"])
                else:
                    row[c.name] = rng.randint(INT_LO, INT_HI)
            rows.append(row)
        data[t.name] = rows
    return data


def build_sqlite(tables: list[GenTable], data: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    for t in tables:
        defs = ", ".join(f"{c.name} {'TEXT' if c.ctype == 'TEXT' else 'INTEGER'}"
                         for c in t.columns)
        cur.execute(f"CREATE TABLE {t.name} ({defs})")
        for row in data.get(t.name, []):
            names = list(row.keys())
            cur.execute(
                f"INSERT INTO {t.name} ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [row[k] for k in names],
            )
    conn.commit()
    return conn


def run_bag(conn: sqlite3.Connection, sql: str) -> Counter:
    """Execute a query and return its output as a bag of value tuples."""
    rows = conn.execute(sql).fetchall()
    return Counter(tuple(r) for r in rows)


def bags_on_db(tables, data, sql1, sql2) -> tuple[Counter, Counter]:
    conn = build_sqlite(tables, data)
    try:
        return run_bag(conn, sql1), run_bag(conn, sql2)
    finally:
        conn.close()


# ── Oracle ───────────────────────────────────────────────────────────────────

def report_failure(title, seed, ddl, sql1, sql2, extra=""):
    print(f"\n{'=' * 70}\nFAILURE [{title}] seed={seed}\n{ddl}\n  v1: {sql1}\n  v2: {sql2}\n{extra}\n{'=' * 70}")


def check_seed(seed: int, bound: int, dbs_per_pair: int) -> tuple[str, bool]:
    """Run one fuzz iteration. Returns (verdict, ok)."""
    rng = random.Random(seed)
    tables = gen_schema(rng)
    ddl = render_ddl(tables)
    base = gen_query(rng, tables)
    mutant, mutation = mutate(rng, base, tables)
    sql1, sql2 = base.render(), mutant.render()

    # Semantics-preserving CTE rewrites of base (base-form ≡ base, so pair
    # invariants hold), cross-checking CTE materialization against SQLite:
    #   ~25% wrap base's FROM table in a CTE (single-table body, joined);
    #   ~20% materialize the WHOLE query as a CTE (aggregating/multi-table/outer
    #        bodies — the cases inlining never could).
    r = rng.random()
    if r < 0.25 and base.join_type not in ("LEFT", "RIGHT"):
        # cte_wrap puts the CTE in FROM; combined with an outer join that's a
        # (correct) scope fail-closed, so only wrap inner/no-join bases here.
        sql1 = cte_wrap(base, tables, rng)
    elif r < 0.45:
        # materialize_wrap keeps any outer join INSIDE the CTE body (allowed).
        sql1 = materialize_wrap(base)

    result = check_equivalence(ddl, sql1, sql2, dialect="sqlite",
                               bound=bound, timeout_ms=60_000)

    if result.status == "error":
        report_failure("unexpected error verdict", seed, ddl, sql1, sql2,
                       f"  error: {result.error_message}")
        return result.status, False

    if result.status == "unknown":
        return result.status, True

    if result.status == "equivalent":
        if mutation != "identity":
            pass  # mutants may legitimately stay equivalent; verify by sampling
        for k in range(dbs_per_pair):
            data = sample_db(random.Random(seed * 10_007 + k), tables, bound)
            b1, b2 = bags_on_db(tables, data, sql1, sql2)
            if b1 != b2:
                report_failure(
                    "FALSE EQUIVALENT — encoding bug", seed, ddl, sql1, sql2,
                    f"  mutation: {mutation}\n  db: {data}\n"
                    f"  v1 bag: {dict(b1)}\n  v2 bag: {dict(b2)}")
                return result.status, False
        return result.status, True

    # divergent
    if mutation == "identity":
        report_failure("identical pair reported divergent", seed, ddl, sql1, sql2)
        return result.status, False
    if not result.counterexample_db:
        report_failure("divergent without counterexample", seed, ddl, sql1, sql2)
        return result.status, False
    b1, b2 = bags_on_db(tables, result.counterexample_db, sql1, sql2)
    if b1 == b2:
        report_failure(
            "SPURIOUS COUNTEREXAMPLE — witness does not reproduce", seed, ddl,
            sql1, sql2,
            f"  witness: {result.counterexample_db}\n  shared bag: {dict(b1)}")
        return result.status, False
    return result.status, True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--seed-start", type=int, default=0)
    ap.add_argument("--bound", type=int, default=2)
    ap.add_argument("--dbs", type=int, default=25,
                    help="sampled databases per 'equivalent' verdict")
    args = ap.parse_args()

    tally = Counter()
    failures = 0
    for seed in range(args.seed_start, args.seed_start + args.seeds):
        try:
            verdict, ok = check_seed(seed, args.bound, args.dbs)
        except Exception as e:
            print(f"\nFAILURE [harness crash] seed={seed}: {type(e).__name__}: {e}")
            failures += 1
            continue
        tally[verdict] += 1
        if not ok:
            failures += 1

    print(f"\n{args.seeds} seeds @ bound={args.bound}: "
          f"{tally['equivalent']} equivalent "
          f"({tally['equivalent'] and args.dbs} DBs sampled each), "
          f"{tally['divergent']} divergent, {tally['unknown']} unknown, "
          f"{tally['error']} error — {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
