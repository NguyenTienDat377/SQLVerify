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
CREATE TABLE departments (
    dept_id INTEGER PRIMARY KEY,
    region TEXT
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

# ── OR / IN / NOT predicates (three-valued Kleene logic, paper Fig. 4/5) ─────

def test_or_predicate_self_equivalent():
    q = "SELECT account_id FROM accounts WHERE balance > 1 OR balance < -5"
    _check(q, q, "equivalent")


def test_or_vs_and_divergent():
    # OR and AND of the same two comparisons are not the same filter.
    _check(
        "SELECT account_id FROM accounts WHERE balance > 1 OR balance < 5",
        "SELECT account_id FROM accounts WHERE balance > 1 AND balance < 5",
        "divergent",
    )


def test_in_list_equals_or_of_equalities():
    # IN (1, 2) is exactly `= 1 OR = 2` — the desugaring must be equivalent.
    _check(
        "SELECT account_id FROM accounts WHERE balance IN (1, 2)",
        "SELECT account_id FROM accounts WHERE balance = 1 OR balance = 2",
        "equivalent",
    )


def test_in_list_vs_single_value_divergent():
    _check(
        "SELECT account_id FROM accounts WHERE balance IN (1, 2)",
        "SELECT account_id FROM accounts WHERE balance = 1",
        "divergent",
    )


def test_not_in_is_de_morgan_conjunction():
    # NOT IN (1,2) ≡ <> 1 AND <> 2 under three-valued logic (a NULL balance is
    # dropped by both — NOT(NULL) is NULL, and <> on a NULL operand is NULL).
    _check(
        "SELECT account_id FROM accounts WHERE balance NOT IN (1, 2)",
        "SELECT account_id FROM accounts WHERE balance <> 1 AND balance <> 2",
        "equivalent",
    )


def test_or_with_is_null_keeps_null_rows():
    # `= 1 OR IS NULL` keeps NULL rows that a bare `= 1` drops → divergent.
    # Guards that OR composes the three-valued halves correctly around NULLs.
    _check(
        "SELECT account_id FROM accounts WHERE balance = 1 OR balance IS NULL",
        "SELECT account_id FROM accounts WHERE balance = 1",
        "divergent",
    )


# ── IN (SELECT ...) membership — the paper's E⃗ ∈ Q semi-join (Fig. 4) ────────
# Uncorrelated, single-column body, WHERE only. Bodies materialised once and
# encoded as a three-valued membership disjunction; NOT IN = NOT(IN) via Kleene.

def test_in_subquery_self_equivalent():
    q = ("SELECT account_id FROM accounts WHERE account_id IN "
         "(SELECT amount FROM transactions)")
    _check(q, q, "equivalent")


def test_in_subquery_body_notnull_filter_equivalent():
    # WHERE keeps a row iff the predicate is TRUE, so a NULL body cell and a
    # missing body cell are indistinguishable for plain IN — filtering the
    # body's NULLs out changes nothing.
    _check(
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT amount FROM transactions)",
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT amount FROM transactions WHERE amount IS NOT NULL)",
        "equivalent",
    )


def test_not_in_null_trap_divergent():
    # The classic NOT IN NULL trap: a single NULL in the body makes NOT IN
    # never-TRUE, so removing the body's NULLs is NOT equivalence-preserving.
    _check(
        "SELECT account_id FROM accounts WHERE account_id NOT IN "
        "(SELECT amount FROM transactions)",
        "SELECT account_id FROM accounts WHERE account_id NOT IN "
        "(SELECT amount FROM transactions WHERE amount IS NOT NULL)",
        "divergent",
    )


def test_in_subquery_same_table_pk_membership_equivalent():
    # account_id IN (SELECT account_id FROM accounts) holds for every present
    # row (PK is non-NULL), so the filter is a no-op — exercises shared-base-
    # table soundness (the body reads the same symbolic rows as the outer query).
    _check(
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT account_id FROM accounts)",
        "SELECT account_id FROM accounts",
        "equivalent",
    )


def test_in_subquery_vs_constant_divergent():
    _check(
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT amount FROM transactions WHERE dept = 1)",
        "SELECT account_id FROM accounts WHERE account_id = 5",
        "divergent",
    )


def test_in_subquery_aggregating_body_self_equivalent():
    q = ("SELECT account_id FROM accounts WHERE account_id IN "
         "(SELECT SUM(amount) FROM transactions GROUP BY dept)")
    _check(q, q, "equivalent")


def test_in_subquery_aggregating_body_vs_flat_divergent():
    _check(
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT SUM(amount) FROM transactions GROUP BY dept)",
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT amount FROM transactions)",
        "divergent",
    )


def test_scalar_subquery_rejected():
    _check(
        "SELECT account_id FROM accounts WHERE account_id = "
        "(SELECT amount FROM transactions)",
        "SELECT account_id FROM accounts WHERE account_id = 5",
        "error", msg_contains="subquer",
    )


def test_in_subquery_multi_column_body_rejected():
    _check(
        "SELECT account_id FROM accounts WHERE account_id IN "
        "(SELECT amount, dept FROM transactions)",
        "SELECT account_id FROM accounts",
        "error", msg_contains="exactly one column",
    )


def test_in_subquery_tuple_lhs_rejected():
    _check(
        "SELECT account_id FROM accounts WHERE (account_id, balance) IN "
        "(SELECT account_id, amount FROM transactions)",
        "SELECT account_id FROM accounts",
        "error", msg_contains="single-column",
    )


def test_correlated_subquery_rejected():
    # An outer-alias reference inside the body is correlated — fails closed when
    # the body is encoded (its alias_map has only the body's own tables).
    _check(
        "SELECT account_id FROM accounts a WHERE a.account_id IN "
        "(SELECT amount FROM transactions t WHERE t.amount = a.balance)",
        "SELECT account_id FROM accounts WHERE account_id = 5",
        "error", msg_contains="alias",
    )


def test_exists_subquery_rejected():
    _check(
        "SELECT account_id FROM accounts WHERE EXISTS "
        "(SELECT tx_id FROM transactions)",
        "SELECT account_id FROM accounts WHERE account_id = 5",
        "error", msg_contains="subquer",
    )


def test_in_subquery_in_having_rejected():
    _check(
        "SELECT dept FROM transactions GROUP BY dept HAVING dept IN "
        "(SELECT account_id FROM accounts)",
        "SELECT dept FROM transactions GROUP BY dept",
        "error", msg_contains="subquer",
    )


def test_in_subquery_type_mismatch_rejected():
    # LHS is TEXT (interned), body column is INTEGER — symbolic equality across
    # the two would be unsound, so it fails closed.
    _check(
        "SELECT account_id FROM accounts WHERE status IN "
        "(SELECT amount FROM transactions)",
        "SELECT account_id FROM accounts",
        "error", msg_contains="incompatible",
    )


# ── Non-recursive CTE inlining (paper's With(Q̃,R⃗,Q); we flatten, not materialize) ─

def test_cte_equals_flat_form():
    # WITH-wrapping a filtered projection must prove equal to the flat query.
    _check(
        "WITH active AS (SELECT account_id AS aid FROM accounts WHERE balance > 100) "
        "SELECT aid FROM active",
        "SELECT account_id AS aid FROM accounts WHERE balance > 100",
        "equivalent",
    )


def test_cte_joined_to_real_table_equals_flat_join():
    _check(
        "WITH a AS (SELECT account_id AS aid FROM accounts WHERE balance > 0) "
        "SELECT a.aid, t.amount FROM a JOIN transactions t ON a.aid = t.account_id",
        "SELECT ac.account_id AS aid, t.amount FROM accounts ac "
        "JOIN transactions t ON ac.account_id = t.account_id WHERE ac.balance > 0",
        "equivalent",
    )


def test_cte_filter_divergent_from_unfiltered():
    _check(
        "WITH a AS (SELECT account_id AS aid FROM accounts WHERE balance > 100) "
        "SELECT aid FROM a",
        "SELECT account_id AS aid FROM accounts",
        "divergent",
    )


def test_aggregating_cte_body_equals_flat():
    # Materialization (VeriEQL's With): an aggregating CTE body — impossible to
    # flatten — proves equal to the flat aggregate query.
    _check(
        "WITH g AS (SELECT dept AS d, SUM(amount) AS s FROM transactions GROUP BY dept) "
        "SELECT d, s FROM g",
        "SELECT dept AS d, SUM(amount) AS s FROM transactions GROUP BY dept",
        "equivalent",
    )


def test_filter_on_cte_aggregate_divergent():
    # Filtering on a CTE's aggregate output — the materialized relation's column
    # is read like any other. Different thresholds must diverge.
    _check(
        "WITH g AS (SELECT dept AS d, SUM(amount) AS s FROM transactions GROUP BY dept) "
        "SELECT d FROM g WHERE s > 5",
        "WITH g AS (SELECT dept AS d, SUM(amount) AS s FROM transactions GROUP BY dept) "
        "SELECT d FROM g WHERE s > 500",
        "divergent",
    )


def test_multi_table_cte_body_equals_flat():
    # A CTE whose body itself joins — also impossible to flatten — materializes
    # and proves equal to the flat join.
    _check(
        "WITH j AS (SELECT a.account_id AS aid, t.amount AS amt FROM accounts a "
        "JOIN transactions t ON a.account_id = t.account_id) SELECT aid, amt FROM j",
        "SELECT a.account_id AS aid, t.amount AS amt FROM accounts a "
        "JOIN transactions t ON a.account_id = t.account_id",
        "equivalent",
    )


def test_cte_inner_joined_to_table_equals_flat():
    _check(
        "WITH a AS (SELECT account_id AS aid FROM accounts WHERE balance > 0) "
        "SELECT a.aid, t.amount FROM a JOIN transactions t ON a.aid = t.account_id",
        "SELECT ac.account_id AS aid, t.amount FROM accounts ac "
        "JOIN transactions t ON ac.account_id = t.account_id WHERE ac.balance > 0",
        "equivalent",
    )


def test_cte_on_outer_join_side_rejected():
    # Scope (FROM + INNER joins): a materialized CTE on an outer-join side needs
    # null-extension handling the inner path doesn't provide. Fail-closed.
    q = ("WITH tx AS (SELECT account_id AS aid FROM transactions) "
         "SELECT a.account_id FROM accounts a LEFT JOIN tx ON a.account_id = tx.aid")
    _check(q, q, "error", msg_contains="outer")


def test_recursive_cte_rejected():
    q = ("WITH RECURSIVE c AS (SELECT account_id FROM accounts) "
         "SELECT account_id FROM c")
    _check(q, q, "error", msg_contains="Recursive")


def test_cte_on_cte_rejected():
    # CTE-on-CTE (a CTE body referencing another CTE) is not supported yet.
    q = ("WITH a AS (SELECT account_id AS aid FROM accounts), "
         "b AS (SELECT aid FROM a) SELECT aid FROM b")
    _check(q, q, "error", msg_contains="another CTE")


def test_cte_over_table_also_joined_to_that_table():
    # A CTE over `accounts` joined back to `accounts` on the PK yields each
    # account once — equal to a plain select. Materialization treats the CTE as
    # an independent relation while sharing the base rows (a case inlining would
    # have rejected as a self-join).
    _check(
        "WITH c AS (SELECT account_id AS aid FROM accounts) "
        "SELECT c.aid, a.balance AS b FROM c JOIN accounts a ON c.aid = a.account_id",
        "SELECT account_id AS aid, balance AS b FROM accounts",
        "equivalent",
    )


def test_select_star_rejected():
    q = "SELECT * FROM accounts"
    _check(q, q, "error", msg_contains="Unsupported SELECT")


def test_select_arithmetic_rejected():
    q = "SELECT balance + 1 AS b FROM accounts"
    _check(q, q, "error", msg_contains="Unsupported")


# ── Multiple INNER joins ─────────────────────────────────────────────────────

def test_three_table_inner_join_self_equivalent():
    q = ("SELECT a.account_id FROM accounts a "
         "JOIN transactions t ON a.account_id = t.account_id "
         "JOIN departments d ON t.dept = d.dept_id "
         "WHERE t.amount > 0")
    _check(q, q, "equivalent")


def test_three_table_inner_join_reorder_equivalent():
    # INNER joins are associative/commutative under bag semantics: stating the
    # joins in a different order yields the same result bag.
    _check(
        "SELECT a.account_id, d.region FROM accounts a "
        "JOIN transactions t ON a.account_id = t.account_id "
        "JOIN departments d ON t.dept = d.dept_id",
        "SELECT a.account_id, d.region FROM departments d "
        "JOIN transactions t ON t.dept = d.dept_id "
        "JOIN accounts a ON a.account_id = t.account_id",
        "equivalent",
    )


def test_three_table_inner_join_dropped_predicate_divergent():
    # Dropping the second join's filter must change the result.
    _check(
        "SELECT a.account_id FROM accounts a "
        "JOIN transactions t ON a.account_id = t.account_id "
        "JOIN departments d ON t.dept = d.dept_id WHERE d.dept_id > 1",
        "SELECT a.account_id FROM accounts a "
        "JOIN transactions t ON a.account_id = t.account_id "
        "JOIN departments d ON t.dept = d.dept_id",
        "divergent",
    )


def test_group_by_joined_table_column_accepted():
    # GROUP BY on a JOINED table's column (previously rejected) now verifies.
    q = ("SELECT d.region, COUNT(*) AS c FROM accounts a "
         "JOIN transactions t ON a.account_id = t.account_id "
         "JOIN departments d ON t.dept = d.dept_id "
         "GROUP BY d.region")
    _check(q, q, "equivalent")


def test_outer_join_plus_inner_join_rejected():
    q = ("SELECT a.account_id FROM accounts a "
         "LEFT JOIN transactions t ON a.account_id = t.account_id "
         "JOIN departments d ON t.dept = d.dept_id")
    _check(q, q, "error", msg_contains="Outer joins are supported only as a single join")


def test_self_join_rejected():
    q = ("SELECT a.account_id FROM accounts a "
         "JOIN accounts b ON a.account_id = b.balance")
    _check(q, q, "error", msg_contains="Self-join")


# ── FULL OUTER JOIN ──────────────────────────────────────────────────────────
# FULL = matched pairs ∪ LEFT's unmatched-FROM rows ∪ RIGHT's unmatched-join
# rows. Both sides are null-extended; ON-vs-WHERE is load-bearing on both.

def test_full_outer_join_self_equivalent():
    # `FULL JOIN` and `FULL OUTER JOIN` parse identically (side == "FULL").
    _check(
        "SELECT a.account_id, t.tx_id FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "SELECT a.account_id, t.tx_id FROM accounts a FULL JOIN "
        "transactions t ON a.account_id = t.account_id",
        "equivalent",
    )


def test_full_outer_join_vs_left_divergent():
    # FULL keeps unmatched transactions (right rows); LEFT drops them.
    _check(
        "SELECT a.account_id, t.tx_id FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "SELECT a.account_id, t.tx_id FROM accounts a LEFT JOIN "
        "transactions t ON a.account_id = t.account_id",
        "divergent",
    )


def test_full_outer_join_vs_right_divergent():
    # Symmetrically, FULL keeps unmatched accounts (left rows); RIGHT drops them.
    _check(
        "SELECT a.account_id, t.tx_id FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "SELECT a.account_id, t.tx_id FROM accounts a RIGHT JOIN "
        "transactions t ON a.account_id = t.account_id",
        "divergent",
    )


def test_full_outer_join_vs_inner_divergent():
    _check(
        "SELECT a.account_id, t.tx_id FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "SELECT a.account_id, t.tx_id FROM accounts a INNER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "divergent",
    )


def test_full_outer_where_on_left_equals_left_join():
    # A WHERE filter on a FROM-table column NULLs out the right-extended rows
    # (their FROM cells are NULL), collapsing FULL to LEFT.
    _check(
        "SELECT a.balance AS b, t.amount AS m FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id WHERE a.balance > 0",
        "SELECT a.balance AS b, t.amount AS m FROM accounts a LEFT JOIN "
        "transactions t ON a.account_id = t.account_id WHERE a.balance > 0",
        "equivalent",
    )


def test_full_outer_where_on_right_equals_right_join():
    # Symmetric: a WHERE filter on a join-table column collapses FULL to RIGHT.
    _check(
        "SELECT a.balance AS b, t.amount AS m FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id WHERE t.amount > 0",
        "SELECT a.balance AS b, t.amount AS m FROM accounts a RIGHT JOIN "
        "transactions t ON a.account_id = t.account_id WHERE t.amount > 0",
        "equivalent",
    )


def test_full_outer_anti_join_on_left_equals_right():
    # WHERE a.account_id IS NULL keeps only the null-extended right rows
    # (unmatched transactions) — the same set RIGHT JOIN + that filter keeps.
    _check(
        "SELECT t.tx_id FROM accounts a FULL OUTER JOIN transactions t "
        "ON a.account_id = t.account_id WHERE a.account_id IS NULL",
        "SELECT t.tx_id FROM accounts a RIGHT JOIN transactions t "
        "ON a.account_id = t.account_id WHERE a.account_id IS NULL",
        "equivalent",
    )


def test_full_outer_count_star_vs_left_divergent():
    _check(
        "SELECT COUNT(*) AS c FROM accounts a FULL OUTER JOIN transactions t "
        "ON a.account_id = t.account_id",
        "SELECT COUNT(*) AS c FROM accounts a LEFT JOIN transactions t "
        "ON a.account_id = t.account_id",
        "divergent",
    )


def test_full_outer_count_col_vs_left_divergent():
    # COUNT(t.amount) still counts the unmatched right rows' amounts, which
    # LEFT never produces — so the two counts differ.
    _check(
        "SELECT COUNT(t.amount) AS c FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "SELECT COUNT(t.amount) AS c FROM accounts a LEFT JOIN "
        "transactions t ON a.account_id = t.account_id",
        "divergent",
    )


def test_full_outer_group_by_self_equivalent():
    # GROUP BY on a non-nullable FROM-table key (accounts.status is NOT NULL):
    # the null-extended right rows form the single extra NULL-key group.
    q = ("SELECT a.status AS s, COUNT(*) AS c FROM accounts a FULL OUTER JOIN "
         "transactions t ON a.account_id = t.account_id GROUP BY a.status")
    _check(q, q, "equivalent")


def test_full_outer_group_by_vs_left_divergent():
    # FULL's extra NULL-key group (from unmatched transactions) has no
    # counterpart under LEFT.
    _check(
        "SELECT a.status AS s, COUNT(*) AS c FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id GROUP BY a.status",
        "SELECT a.status AS s, COUNT(*) AS c FROM accounts a LEFT JOIN "
        "transactions t ON a.account_id = t.account_id GROUP BY a.status",
        "divergent",
    )


def test_full_outer_group_by_nullable_key_rejected():
    # accounts.balance is nullable; a matched row could carry a NULL key and
    # would have to merge with the extra NULL group — not modelled, fail closed.
    q = ("SELECT a.balance AS b, COUNT(*) AS c FROM accounts a FULL OUTER JOIN "
         "transactions t ON a.account_id = t.account_id GROUP BY a.balance")
    _check(q, q, "error", msg_contains="non-nullable")


def test_full_outer_group_by_joined_table_key_rejected():
    q = ("SELECT t.dept AS d, COUNT(*) AS c FROM accounts a FULL OUTER JOIN "
         "transactions t ON a.account_id = t.account_id GROUP BY t.dept")
    _check(q, q, "error", msg_contains="GROUP BY only on columns of the FROM table")


def test_full_outer_inside_cte_body_equals_flat():
    # A FULL join is a valid CTE body (materialized); the reader sees a plain
    # relation, so wrapping it changes nothing.
    _check(
        "WITH m AS (SELECT a.account_id AS i, t.tx_id AS x FROM accounts a "
        "FULL OUTER JOIN transactions t ON a.account_id = t.account_id) "
        "SELECT m.i, m.x FROM m",
        "SELECT a.account_id AS i, t.tx_id AS x FROM accounts a FULL OUTER JOIN "
        "transactions t ON a.account_id = t.account_id",
        "equivalent",
    )


def test_full_outer_join_on_cte_relation_rejected():
    # A materialized CTE relation on an outer-join side has no null-extension
    # for its cells — fail closed (same guard as LEFT/RIGHT).
    q = ("WITH m AS (SELECT account_id AS i FROM accounts) "
         "SELECT m.i, t.tx_id FROM m FULL OUTER JOIN transactions t "
         "ON m.i = t.account_id")
    _check(q, q, "error", msg_contains="CTE relation combined with an outer")


def test_full_outer_plus_inner_join_rejected():
    q = ("SELECT a.account_id FROM accounts a "
         "FULL OUTER JOIN transactions t ON a.account_id = t.account_id "
         "JOIN departments d ON t.dept = d.dept_id")
    _check(q, q, "error", msg_contains="Outer joins are supported only as a single join")


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
