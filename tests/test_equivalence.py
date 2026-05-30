"""
tests/test_equivalence.py

Smoke tests for core/equivalence.py — each is a SQL query pair that must come
back as either `equivalent` or `divergent`. Run with `pytest tests/`.
"""

from core.equivalence import check_equivalence


def test_column_vs_column_filter_divergent():
    """WHERE b = a (column compared to another column) must change results.

    Regression test: the encoder used to handle only column-vs-literal
    predicates, so a column-vs-column filter was silently dropped. That made
    the filtered query look identical to the unfiltered one and produced a
    false `equivalent` verdict. With column-vs-column predicates encoded, the
    two queries below differ on any row where a != b, so the verdict must be
    `divergent`.
    """
    ddl = """
        CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER);
    """
    query_a = "SELECT t.id FROM t"
    query_b = "SELECT t.id FROM t WHERE t.b = t.a"

    result = check_equivalence(ddl, query_a, query_b)
    assert result.status == "divergent", result.error_message


def test_column_vs_column_filter_equivalent():
    """The same column-vs-column filter on both sides must stay equivalent.

    Guards against the fix over-reporting divergence: an identical `WHERE b = a`
    on both queries should still verify as `equivalent`.
    """
    ddl = """
        CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER);
    """
    query_a = "SELECT t.id FROM t WHERE t.b = t.a"
    query_b = "SELECT t.id FROM t WHERE t.a = t.b"

    result = check_equivalence(ddl, query_a, query_b)
    assert result.status == "equivalent", result.error_message


if __name__ == "__main__":
    test_column_vs_column_filter_divergent()
    test_column_vs_column_filter_equivalent()
    print("ok")
