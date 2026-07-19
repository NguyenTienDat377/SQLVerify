"""
tests/ddl_parser_alter_test.py

Tests for the ALTER TABLE folding and fail-closed rejection added to
core/ddl_parser.py.

Before this, parse_ddl() silently skipped any statement that wasn't
CREATE TABLE — including ALTER TABLE, which real Flyway migration
directories are full of (V2__add_not_null.sql etc). Concatenating such a
directory and parsing it produced a SchemaModel *weaker* than the real
schema (a dropped ALTER TABLE ... NOT NULL, a dropped FK), which is the one
failure mode a verifier must never have: it can turn a false "equivalent"
into a passed check. This suite pins two things:

  1. Every ALTER TABLE form Flyway migrations actually use folds correctly,
     in statement order, into the SchemaModel core/sql_encoder.py consumes.
  2. Everything outside the supported subset — ALTER COLUMN TYPE, DROP
     CONSTRAINT, CREATE VIEW, DML, ALTER-before-CREATE, duplicate/missing
     columns — is rejected with ValueError, never silently dropped.

Run directly (no pytest needed):
    .venv/bin/python tests/ddl_parser_alter_test.py
or with pytest:
    .venv/bin/python -m pytest tests/ddl_parser_alter_test.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ddl_parser import parse_ddl


def _parse(ddl, dialect="postgres"):
    return parse_ddl(ddl, dialect=dialect)


def _expect_reject(ddl, needle, dialect="postgres"):
    try:
        _parse(ddl, dialect=dialect)
    except ValueError as e:
        assert needle.lower() in str(e).lower(), f"expected {needle!r} in: {e}"
        return
    raise AssertionError(f"expected ValueError containing {needle!r}, DDL parsed cleanly")


# ── ADD COLUMN ────────────────────────────────────────────────────────────────

def test_add_column():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY, name TEXT);
        ALTER TABLE users ADD COLUMN age INT;
    """)
    t = schema.get_table("users")
    col = t.get_column("age")
    assert col is not None
    assert col.col_type == "INTEGER"
    assert col.nullable is True


def test_add_column_not_null():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY);
        ALTER TABLE users ADD COLUMN age INT NOT NULL;
    """)
    assert schema.get_table("users").get_column("age").nullable is False


def test_add_column_duplicate_rejected():
    _expect_reject("""
        CREATE TABLE users (id INT PRIMARY KEY);
        ALTER TABLE users ADD COLUMN id INT;
    """, "already exists")


def test_multiple_actions_in_one_statement():
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t ADD COLUMN a INT, ADD COLUMN b TEXT;
    """)
    t = schema.get_table("t")
    assert t.get_column("a") is not None
    assert t.get_column("b") is not None


# ── ALTER COLUMN SET/DROP NOT NULL ──────────────────────────────────────────

def test_set_not_null():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY, name TEXT);
        ALTER TABLE users ALTER COLUMN name SET NOT NULL;
    """)
    assert schema.get_table("users").get_column("name").nullable is False


def test_drop_not_null():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL);
        ALTER TABLE users ALTER COLUMN name DROP NOT NULL;
    """)
    assert schema.get_table("users").get_column("name").nullable is True


def test_statement_order_matters():
    """ADD COLUMN then SET NOT NULL must see the column the earlier
    statement introduced — folding is order-dependent, not a merge."""
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t ADD COLUMN age INT;
        ALTER TABLE t ALTER COLUMN age SET NOT NULL;
    """)
    assert schema.get_table("t").get_column("age").nullable is False


def test_alter_unknown_column_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t ALTER COLUMN ghost SET NOT NULL;
    """, "no such column")


def test_alter_column_type_change_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t ALTER COLUMN id TYPE BIGINT;
    """, "type")


def test_drop_not_null_on_primary_key_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t ALTER COLUMN id DROP NOT NULL;
    """, "primary key column")


# ── ADD CONSTRAINT ───────────────────────────────────────────────────────────

def test_add_constraint_foreign_key():
    schema = _parse("""
        CREATE TABLE depts (id INT PRIMARY KEY);
        CREATE TABLE users (id INT PRIMARY KEY, dept_id INT);
        ALTER TABLE users ADD CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES depts(id);
    """)
    fks = schema.get_table("users").foreign_keys
    assert len(fks) == 1
    assert fks[0].columns == ["dept_id"]
    assert fks[0].references_table == "depts"


def test_add_constraint_primary_key():
    schema = _parse("""
        CREATE TABLE t (id INT);
        ALTER TABLE t ADD PRIMARY KEY (id);
    """)
    t = schema.get_table("t")
    assert t.primary_key_columns == ["id"]
    col = t.get_column("id")
    assert col.primary_key is True
    assert col.nullable is False


def test_add_constraint_primary_key_unknown_column_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT);
        ALTER TABLE t ADD PRIMARY KEY (ghost);
    """, "no such column")


def test_add_constraint_check_is_accepted_as_noop():
    """CHECK is parsed but not encoded into Z3 on CREATE TABLE too — ADD
    CONSTRAINT CHECK must not be rejected just because it arrived via ALTER."""
    schema = _parse("""
        CREATE TABLE t (id INT, age INT);
        ALTER TABLE t ADD CONSTRAINT chk CHECK (age > 0);
    """)
    assert schema.get_table("t").get_column("age") is not None


def test_add_constraint_unique_is_accepted_as_noop():
    schema = _parse("""
        CREATE TABLE t (id INT, email TEXT);
        ALTER TABLE t ADD CONSTRAINT uq UNIQUE (email);
    """)
    assert schema.get_table("t").get_column("email") is not None


# ── DROP COLUMN / DROP CONSTRAINT ───────────────────────────────────────────

def test_drop_column():
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY, name TEXT);
        ALTER TABLE t DROP COLUMN name;
    """)
    assert schema.get_table("t").get_column("name") is None


def test_drop_column_removes_dependent_foreign_key():
    schema = _parse("""
        CREATE TABLE depts (id INT PRIMARY KEY);
        CREATE TABLE users (id INT PRIMARY KEY, dept_id INT REFERENCES depts(id));
        ALTER TABLE users DROP COLUMN dept_id;
    """)
    t = schema.get_table("users")
    assert t.get_column("dept_id") is None
    assert t.foreign_keys == []


def test_drop_column_removes_from_primary_key_columns():
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY, tag TEXT);
        ALTER TABLE t DROP COLUMN tag;
    """)
    assert "tag" not in schema.get_table("t").primary_key_columns


def test_drop_unknown_column_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t DROP COLUMN ghost;
    """, "no such column")


def test_drop_constraint_rejected():
    """Constraint names aren't tracked on ForeignKey/Column, so DROP
    CONSTRAINT can't safely identify what to remove — fail closed."""
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t DROP CONSTRAINT fk_ghost;
    """, "constraint")


# ── RENAME TABLE / RENAME COLUMN ────────────────────────────────────────────

def test_rename_table():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY);
        ALTER TABLE users RENAME TO members;
    """)
    assert schema.get_table("users") is None
    assert schema.get_table("members") is not None
    assert schema.get_table("members").name == "members"


def test_rename_table_to_existing_name_rejected():
    _expect_reject("""
        CREATE TABLE a (id INT PRIMARY KEY);
        CREATE TABLE b (id INT PRIMARY KEY);
        ALTER TABLE a RENAME TO b;
    """, "already exists")


def test_rename_column():
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY, name TEXT);
        ALTER TABLE users RENAME COLUMN name TO full_name;
    """)
    t = schema.get_table("users")
    assert t.get_column("name") is None
    assert t.get_column("full_name") is not None


def test_rename_column_updates_primary_key_columns():
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY);
        ALTER TABLE t RENAME COLUMN id TO pk;
    """)
    t = schema.get_table("t")
    assert t.primary_key_columns == ["pk"]
    assert t.get_column("pk").primary_key is True


def test_rename_column_updates_foreign_key_columns():
    schema = _parse("""
        CREATE TABLE depts (id INT PRIMARY KEY);
        CREATE TABLE users (id INT PRIMARY KEY, dept_id INT REFERENCES depts(id));
        ALTER TABLE users RENAME COLUMN dept_id TO department_id;
    """)
    fk = schema.get_table("users").foreign_keys[0]
    assert fk.columns == ["department_id"]


def test_rename_then_alter_uses_new_name():
    """A later ALTER TABLE must address the table by the name a prior RENAME
    gave it — this is what makes folding order-sensitive rather than a bag
    of independent edits."""
    schema = _parse("""
        CREATE TABLE users (id INT PRIMARY KEY);
        ALTER TABLE users RENAME TO members;
        ALTER TABLE members ADD COLUMN joined_at TIMESTAMP;
    """)
    assert schema.get_table("members").get_column("joined_at") is not None


def test_alter_by_old_name_after_rename_rejected():
    _expect_reject("""
        CREATE TABLE users (id INT PRIMARY KEY);
        ALTER TABLE users RENAME TO members;
        ALTER TABLE users ADD COLUMN x INT;
    """, "doesn't exist yet")


# ── Fail-closed: statements outside the supported subset ───────────────────

def test_alter_before_create_rejected():
    _expect_reject("ALTER TABLE ghost ADD COLUMN x INT;", "doesn't exist yet")


def test_create_view_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        CREATE VIEW v AS SELECT id FROM t;
    """, "view")


def test_create_index_is_skipped_not_rejected():
    """Indexes never change query results, so unlike CREATE VIEW this one
    stays a safe no-op skip rather than a fail-closed rejection."""
    schema = _parse("""
        CREATE TABLE t (id INT PRIMARY KEY);
        CREATE INDEX idx_id ON t(id);
    """)
    assert schema.get_table("t") is not None


def test_dml_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        DELETE FROM t;
    """, "unsupported")


def test_alter_view_rejected():
    _expect_reject("""
        CREATE TABLE t (id INT PRIMARY KEY);
        CREATE VIEW v AS SELECT id FROM t;
        ALTER VIEW v RENAME TO v2;
    """, "view")


# ── Runner (no pytest required) ──────────────────────────────────────────────

def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}\n        {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}\n        {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
