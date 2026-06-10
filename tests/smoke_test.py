"""
tests/smoke_test.py

Smoke tests for core/equivalence.py covering the fail-closed parser, the
ON-vs-WHERE outer-join semantics, and RIGHT JOIN support.

Run directly (no pytest needed):
    .venv/bin/python tests/smoke_test.py
or with pytest:
    .venv/bin/python -m pytest tests/smoke_test.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.equivalence import check_equivalence

DDL = """
CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    balance INTEGER
);
CREATE TABLE transactions (
    tx_id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(account_id),
    amount INTEGER,
    dept INTEGER
);
"""


def _check(sql_v1, sql_v2, expected_status, msg_contains=None):
    result = check_equivalence(DDL, sql_v1, sql_v2, dialect="postgres", timeout_ms=30_000)
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


# ── Baseline ─────────────────────────────────────────────────────────────────

def test_identical_queries_equivalent():
    q = "SELECT account_id FROM accounts WHERE balance > 100"
    _check(q, q, "equivalent")


def test_trivially_different_divergent():
    _check(
        "SELECT account_id FROM accounts WHERE balance > 100",
        "SELECT account_id FROM accounts WHERE balance > 200",
        "divergent",
    )


# ── LEFT JOIN: ON vs WHERE (fix #2) ──────────────────────────────────────────

def test_left_join_where_on_right_equals_inner_join():
    # A WHERE filter on the right table eliminates null-extended rows, so
    # LEFT JOIN + WHERE r.col = lit is equivalent to INNER JOIN + same WHERE.
    _check(
        "SELECT a.account_id FROM accounts a LEFT JOIN transactions t "
        "ON a.account_id = t.account_id WHERE t.amount > 5",
        "SELECT a.account_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id WHERE t.amount > 5",
        "equivalent",
    )


def test_left_join_vs_inner_join_divergent():
    _check(
        "SELECT a.account_id FROM accounts a LEFT JOIN transactions t "
        "ON a.account_id = t.account_id",
        "SELECT a.account_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id",
        "divergent",
    )


def test_left_join_anti_join_idiom_divergent_from_inner():
    # WHERE t.tx_id IS NULL keeps exactly the null-extended (unmatched) rows.
    _check(
        "SELECT a.account_id FROM accounts a LEFT JOIN transactions t "
        "ON a.account_id = t.account_id WHERE t.tx_id IS NULL",
        "SELECT a.account_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id",
        "divergent",
    )


# ── RIGHT JOIN (fix #3) ──────────────────────────────────────────────────────

def test_right_join_equals_swapped_left_join():
    _check(
        "SELECT t.tx_id FROM accounts a RIGHT JOIN transactions t "
        "ON a.account_id = t.account_id",
        "SELECT t.tx_id FROM transactions t LEFT JOIN accounts a "
        "ON a.account_id = t.account_id",
        "equivalent",
    )


def test_right_join_where_on_left_equals_inner_join():
    _check(
        "SELECT t.tx_id FROM accounts a RIGHT JOIN transactions t "
        "ON a.account_id = t.account_id WHERE a.balance > 0",
        "SELECT t.tx_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id WHERE a.balance > 0",
        "equivalent",
    )


def test_right_join_vs_inner_join_divergent():
    _check(
        "SELECT t.tx_id FROM accounts a RIGHT JOIN transactions t "
        "ON a.account_id = t.account_id",
        "SELECT t.tx_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id",
        "divergent",
    )


# ── Fail-closed parsing (fix #1) ─────────────────────────────────────────────

def test_or_predicate_rejected():
    q = "SELECT account_id FROM accounts WHERE balance > 1 OR balance < -5"
    _check(q, q, "error", msg_contains="Unsupported")


def test_in_predicate_rejected():
    q = "SELECT account_id FROM accounts WHERE balance IN (1, 2)"
    _check(q, q, "error", msg_contains="Unsupported")


def test_select_star_rejected():
    q = "SELECT * FROM accounts"
    _check(q, q, "error", msg_contains="Unsupported SELECT")


def test_select_arithmetic_rejected():
    q = "SELECT balance + 1 AS b FROM accounts"
    _check(q, q, "error", msg_contains="Unsupported")


def test_multiple_joins_rejected():
    q = ("SELECT a.account_id FROM accounts a "
         "JOIN transactions t ON a.account_id = t.account_id "
         "JOIN transactions u ON a.account_id = u.account_id")
    _check(q, q, "error", msg_contains="at most one JOIN")


def test_self_join_rejected():
    q = ("SELECT a.account_id FROM accounts a "
         "JOIN accounts b ON a.account_id = b.balance")
    _check(q, q, "error", msg_contains="Self-join")


def test_full_outer_join_rejected():
    q = ("SELECT a.account_id FROM accounts a FULL OUTER JOIN transactions t "
         "ON a.account_id = t.account_id")
    _check(q, q, "error", msg_contains="FULL OUTER")


def test_count_distinct_rejected():
    q = "SELECT COUNT(DISTINCT account_id) AS c FROM accounts"
    _check(q, q, "error", msg_contains="DISTINCT")


def test_select_distinct_rejected():
    q = "SELECT DISTINCT account_id FROM accounts"
    _check(q, q, "error", msg_contains="DISTINCT")


def test_limit_rejected():
    q = "SELECT account_id FROM accounts LIMIT 5"
    _check(q, q, "error", msg_contains="LIMIT")


def test_string_ordering_rejected():
    q = "SELECT account_id FROM accounts WHERE status > 'active'"
    _check(q, q, "error", msg_contains="Ordering comparison")


def test_unknown_column_rejected():
    q = "SELECT account_id FROM accounts WHERE no_such_col = 1"
    _check(q, q, "error", msg_contains="not found")


# ── Previously dropped-silently constructs now encoded correctly ─────────────

def test_negative_literal_encoded():
    # WHERE balance > -1 vs WHERE balance >= 0 are equivalent over integers;
    # the old parser silently dropped negative literals entirely.
    _check(
        "SELECT account_id FROM accounts WHERE balance > -1",
        "SELECT account_id FROM accounts WHERE balance >= 0",
        "equivalent",
    )
    _check(
        "SELECT account_id FROM accounts WHERE balance > -1",
        "SELECT account_id FROM accounts WHERE balance > 0",
        "divergent",
    )


def test_unqualified_join_table_column_in_where():
    # 'amount' lives on the join table; the old resolver attributed it to the
    # FROM table and silently dropped the filter (false 'equivalent').
    _check(
        "SELECT a.account_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id WHERE amount > 5",
        "SELECT a.account_id FROM accounts a INNER JOIN transactions t "
        "ON a.account_id = t.account_id",
        "divergent",
    )


def test_having_aggregate_not_in_select():
    # HAVING COUNT(*) > 1 has no matching SELECT alias; the old encoder
    # silently dropped it (false 'equivalent').
    _check(
        "SELECT dept, SUM(amount) AS s FROM transactions GROUP BY dept "
        "HAVING COUNT(*) > 1",
        "SELECT dept, SUM(amount) AS s FROM transactions GROUP BY dept",
        "divergent",
    )


# ── Aggregates & GROUP BY sanity ─────────────────────────────────────────────

def test_count_star_vs_count_col_divergent():
    _check(
        "SELECT COUNT(*) AS c FROM transactions",
        "SELECT COUNT(amount) AS c FROM transactions",
        "divergent",
    )


def test_where_vs_having_filter_divergent():
    _check(
        "SELECT dept, COUNT(*) AS c FROM transactions WHERE amount > 0 GROUP BY dept",
        "SELECT dept, COUNT(*) AS c FROM transactions GROUP BY dept",
        "divergent",
    )


def test_coalesce_sum_vs_plain_sum_divergent():
    _check(
        "SELECT dept, COALESCE(SUM(amount), 0) AS s FROM transactions GROUP BY dept",
        "SELECT dept, SUM(amount) AS s FROM transactions GROUP BY dept",
        "divergent",
    )


def test_group_by_aggregate_equivalent():
    _check(
        "SELECT dept, COUNT(*) AS c FROM transactions GROUP BY dept",
        "SELECT dept, COUNT(*) AS c FROM transactions GROUP BY dept",
        "equivalent",
    )


def test_divergent_witness_reproduces():
    result = _check(
        "SELECT account_id FROM accounts WHERE balance > 100",
        "SELECT account_id FROM accounts WHERE balance >= 100",
        "divergent",
    )
    assert result.counterexample_db, "expected a counterexample DB"
    assert result.query_v1_output != result.query_v2_output, (
        "witness outputs should differ"
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
