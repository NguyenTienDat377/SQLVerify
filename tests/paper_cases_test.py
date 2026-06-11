"""
tests/paper_cases_test.py

Regression tests derived from the VeriEQL paper (3649849.pdf, OOPSLA 2024)
to validate SQLVerify's encoding against the paper's semantics:

  - Fig. 6   join semantics (null extension, multiplicity preservation)
  - Fig. 8   integrity constraints (IC-PK incl. composite keys, IC-FK, IC-NN)
  - Fig. 9   three-valued predicate logic
  - §3.3     GroupBy = Dedup + Eval (NULL keys form one group)
  - §4.5     bag equivalence via tuple multiplicity (Eqns 1-2)

Each case is chosen so the expected verdict follows from a specific paper rule;
a wrong encoding of that rule flips the verdict. See docs/encoding_audit.md
for the rule-by-rule audit these tests back up.

Run directly (no pytest needed):
    .venv/bin/python tests/paper_cases_test.py
or with pytest:
    .venv/bin/python -m pytest tests/paper_cases_test.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ddl_parser import parse_ddl
from core.equivalence import check_equivalence

# Paper-flavoured EMP/DEPT schema (Fig. 6) plus the Friendship relation from
# the overview example (Fig. 2), which carries a composite primary key.
DDL = """
CREATE TABLE dept (
    did INTEGER PRIMARY KEY,
    dname TEXT
);
CREATE TABLE emp (
    eid INTEGER PRIMARY KEY,
    did INTEGER REFERENCES dept(did),
    sal INTEGER,
    age INTEGER NOT NULL
);
CREATE TABLE friendship (
    uid INTEGER NOT NULL,
    fid INTEGER NOT NULL,
    weight INTEGER,
    PRIMARY KEY (uid, fid)
);
"""

# Same emp/dept shape but WITHOUT the foreign key — used to show that the
# FK-dependent equivalences below really do hinge on the IC-FK encoding.
DDL_NO_FK = """
CREATE TABLE dept2 (
    did INTEGER PRIMARY KEY
);
CREATE TABLE emp2 (
    eid INTEGER PRIMARY KEY,
    did INTEGER
);
"""


def _check(sql_v1, sql_v2, expected_status, ddl=DDL, msg_contains=None):
    result = check_equivalence(ddl, sql_v1, sql_v2, dialect="postgres", timeout_ms=30_000)
    assert result.status == expected_status, (
        f"expected {expected_status}, got {result.status} "
        f"(error={result.error_message}, reason={result.divergence_reason})\n"
        f"  v1: {sql_v1}\n  v2: {sql_v2}"
    )
    if msg_contains:
        assert msg_contains.lower() in (result.error_message or "").lower(), (
            f"expected error containing '{msg_contains}', got: {result.error_message}"
        )
    return result


# ── IC-PK Φ₁ (Fig. 8): PK attributes are non-NULL ───────────────────────────
# The Φ₂ uniqueness encoding compares raw values without NULL guards; that is
# sound only because the DDL parser forces PK columns to nullable=False.
# This test pins the dependency (audit row #4/#5).

def test_pk_columns_forced_non_null():
    schema = parse_ddl(DDL, dialect="postgres")
    assert schema.get_table("dept").get_column("did").nullable is False
    assert schema.get_table("emp").get_column("eid").nullable is False
    fr = schema.get_table("friendship")
    assert sorted(fr.primary_key_columns) == ["fid", "uid"], (
        f"composite PK not collected: {fr.primary_key_columns}"
    )
    assert fr.get_column("uid").nullable is False
    assert fr.get_column("fid").nullable is False


# ── IC-PK Φ₂ (Fig. 8): composite keys — tuples must differ on ≥1 attribute ──
# A divergence here is reachable only via two friendship rows sharing uid
# (differing on fid). An over-constrained PK encoding (per-column inequality)
# would forbid that database and yield a false 'equivalent'.

def test_composite_pk_admits_rows_sharing_one_key_column():
    _check(
        "SELECT uid, COUNT(*) AS c FROM friendship GROUP BY uid HAVING COUNT(*) > 1",
        "SELECT uid, COUNT(*) AS c FROM friendship GROUP BY uid HAVING COUNT(*) > 2",
        "divergent",
    )


# ── IC-FK + IC-PK + bag semantics: join multiplicity (Fig. 6 / Eqns 1-2) ─────
# Every non-NULL emp.did references an existing dept row (IC-FK) and dept.did
# is unique (IC-PK), so an INNER JOIN matches each emp row exactly once.

def test_inner_join_on_fk_equals_not_null_filter():
    _check(
        "SELECT e.did FROM emp e INNER JOIN dept d ON e.did = d.did",
        "SELECT did FROM emp WHERE did IS NOT NULL",
        "equivalent",
    )


def test_left_join_on_fk_diverges_from_not_null_filter():
    # LEFT JOIN additionally keeps NULL-did rows (null-extended), so the same
    # pair flips to divergent.
    _check(
        "SELECT e.did FROM emp e LEFT JOIN dept e2 ON e.did = e2.did",
        "SELECT did FROM emp WHERE did IS NOT NULL",
        "divergent",
    )


def test_left_join_on_unique_key_preserves_left_rows():
    # PK uniqueness on dept.did means LEFT JOIN never duplicates an emp row,
    # and null extension never drops one: SELECT of a left column is a no-op.
    _check(
        "SELECT e.eid FROM emp e LEFT JOIN dept d ON e.did = d.did",
        "SELECT eid FROM emp",
        "equivalent",
    )


# ── Three-valued ON + IC-FK: the anti-join idiom (Fig. 2 / §3.3) ─────────────
# NULL join keys never satisfy ON (NULL = x is NULL, not TRUE). With the FK in
# force, every non-NULL emp.did finds its dept, so the anti-join keeps exactly
# the NULL-did rows.

def test_fk_antijoin_equals_is_null_filter():
    _check(
        "SELECT e.eid FROM emp e LEFT JOIN dept d ON e.did = d.did "
        "WHERE d.did IS NULL",
        "SELECT eid FROM emp WHERE did IS NULL",
        "equivalent",
    )


def test_antijoin_without_fk_diverges_from_is_null_filter():
    # Same pair on the FK-less schema: a dangling non-NULL emp2.did is now a
    # legal database, and the anti-join keeps it while the IS NULL filter
    # does not.
    _check(
        "SELECT e.eid FROM emp2 e LEFT JOIN dept2 d ON e.did = d.did "
        "WHERE d.did IS NULL",
        "SELECT eid FROM emp2 WHERE did IS NULL",
        "divergent",
        ddl=DDL_NO_FK,
    )


# ── Three-valued predicate logic (Fig. 9) ────────────────────────────────────

def test_not_of_equality_is_neq():
    _check(
        "SELECT eid FROM emp WHERE sal <> 5",
        "SELECT eid FROM emp WHERE NOT (sal = 5)",
        "equivalent",
    )


def test_neq_already_excludes_null():
    # sal <> 5 is non-TRUE on NULL, so the explicit IS NOT NULL is redundant.
    _check(
        "SELECT eid FROM emp WHERE sal <> 5",
        "SELECT eid FROM emp WHERE sal IS NOT NULL AND sal <> 5",
        "equivalent",
    )


def test_self_equality_filters_nulls_on_nullable_column():
    # sal = sal is NULL (not TRUE) when sal is NULL — the WHERE drops the row.
    _check(
        "SELECT eid FROM emp WHERE sal = sal",
        "SELECT eid FROM emp",
        "divergent",
    )


def test_self_equality_is_noop_on_not_null_column():
    # age is NOT NULL (IC-NN), so age = age is always TRUE.
    _check(
        "SELECT eid FROM emp WHERE age = age",
        "SELECT eid FROM emp",
        "equivalent",
    )


# ── Aggregates and NULL (§3.3, Fig. 5 Eval) ──────────────────────────────────

def test_count_star_equals_count_of_not_null_column():
    _check(
        "SELECT COUNT(*) AS c FROM emp",
        "SELECT COUNT(age) AS c FROM emp",
        "equivalent",
    )


def test_count_star_diverges_from_count_of_nullable_column():
    _check(
        "SELECT COUNT(*) AS c FROM emp",
        "SELECT COUNT(sal) AS c FROM emp",
        "divergent",
    )


def test_ungrouped_sum_is_null_on_empty_input():
    # SUM over zero contributing rows is NULL; COALESCE turns it into 0.
    _check(
        "SELECT SUM(sal) AS s FROM emp",
        "SELECT COALESCE(SUM(sal), 0) AS s FROM emp",
        "divergent",
    )


# ── GroupBy = Dedup + Eval (§3.3): NULL keys form one group ──────────────────

def test_null_group_keys_form_a_group():
    _check(
        "SELECT did, COUNT(*) AS c FROM emp GROUP BY did",
        "SELECT did, COUNT(*) AS c FROM emp WHERE did IS NOT NULL GROUP BY did",
        "divergent",
    )


def test_group_by_collapses_duplicates():
    # Bag semantics (Eqn 2): same distinct values, different multiplicities.
    _check(
        "SELECT did FROM emp GROUP BY did",
        "SELECT did FROM emp",
        "divergent",
    )


# ── Fail-closed: invalid grouped projection must be rejected ─────────────────
# Standard SQL (and the paper's static analysis, §3.2) rejects a bare SELECT
# column that is not a group key; encoding it would invent deterministic
# semantics for an ambiguous query.

def test_bare_select_column_not_in_group_by_rejected():
    q = "SELECT sal FROM emp GROUP BY did"
    _check(q, q, "error", msg_contains="must appear in GROUP BY")


# ── Finite domain window: literals must be reachable ─────────────────────────

def test_domain_window_distinguishes_strict_vs_inclusive():
    _check(
        "SELECT eid FROM emp WHERE sal > 100",
        "SELECT eid FROM emp WHERE sal >= 100",
        "divergent",
    )


if __name__ == "__main__":
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
