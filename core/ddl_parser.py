"""
core/ddl_parser.py

Parses a Flyway-style DDL file into a SchemaModel: one or more CREATE TABLE
statements, plus any ALTER TABLE statements folded in statement order (as a
real Flyway migration history would apply them).

Handles:
  - Inline and out-of-line PRIMARY KEY
  - Inline and out-of-line FOREIGN KEY
  - NOT NULL constraints
  - CHECK constraints (inline; parsed but not encoded into Z3)
  - DEFAULT values
  - Multiple SQL dialects via sqlglot (postgres, mysql, sqlite, etc.)
  - CREATE INDEX (skipped — never changes query results)
  - ALTER TABLE: ADD COLUMN, ALTER COLUMN SET/DROP NOT NULL, ADD CONSTRAINT
    (FOREIGN KEY / PRIMARY KEY / CHECK / UNIQUE), DROP COLUMN, RENAME TO,
    RENAME COLUMN

Fails closed on anything else (CREATE VIEW, ALTER COLUMN TYPE, DROP
CONSTRAINT, DML, ...) rather than silently ignoring it: a dropped
schema-defining statement leaves the encoded schema weaker than reality,
which is exactly how a verifier ends up proving a false "equivalent".

Usage:
    schema = parse_ddl(ddl_text, dialect="postgres")
"""

import sqlglot
import sqlglot.expressions as exp
from sqlglot.expressions import DataType
from typing import ClassVar

from core.models import Column, ForeignKey, SchemaModel, Table

# ── Type normalization ───────────────────────────────────────────────────────
# Maps sqlglot DataType.Type to simplified Z3-friendly type strings.
# Z3 uses these to bound variable domains during encoding.

_TYPE_MAP: dict[DataType.Type, str] = {  # type: ignore[valid-type]
    # Integer family
    exp.DataType.Type.INT: "INTEGER",
    exp.DataType.Type.BIGINT: "INTEGER",
    exp.DataType.Type.SMALLINT: "INTEGER",
    exp.DataType.Type.TINYINT: "INTEGER",
    exp.DataType.Type.SERIAL: "INTEGER",
    exp.DataType.Type.BIGSERIAL: "INTEGER",
    exp.DataType.Type.SMALLSERIAL: "INTEGER",
    exp.DataType.Type.UBIGINT: "INTEGER",
    exp.DataType.Type.USMALLINT: "INTEGER",
    exp.DataType.Type.UTINYINT: "INTEGER",

    # Real / decimal family
    exp.DataType.Type.FLOAT: "REAL",
    exp.DataType.Type.DOUBLE: "REAL",
    exp.DataType.Type.DECIMAL: "REAL",
    exp.DataType.Type.BIGDECIMAL: "REAL",
    exp.DataType.Type.UDECIMAL: "REAL",
    exp.DataType.Type.UDOUBLE: "REAL",
    exp.DataType.Type.DECFLOAT: "REAL",

    # Text family
    exp.DataType.Type.TEXT: "TEXT",
    exp.DataType.Type.VARCHAR: "TEXT",
    exp.DataType.Type.CHAR: "TEXT",
    exp.DataType.Type.NVARCHAR: "TEXT",
    exp.DataType.Type.NCHAR: "TEXT",
    exp.DataType.Type.BPCHAR: "TEXT",
    exp.DataType.Type.TINYTEXT: "TEXT",
    exp.DataType.Type.MEDIUMTEXT: "TEXT",
    exp.DataType.Type.LONGTEXT: "TEXT",

    # Boolean
    exp.DataType.Type.BOOLEAN: "BOOLEAN",
    exp.DataType.Type.BIT: "BOOLEAN",

    # Timestamp / date family
    exp.DataType.Type.TIMESTAMP: "TIMESTAMP",
    exp.DataType.Type.TIMESTAMPTZ: "TIMESTAMP",
    exp.DataType.Type.TIMESTAMPNTZ: "TIMESTAMP",
    exp.DataType.Type.TIMESTAMPLTZ: "TIMESTAMP",
    exp.DataType.Type.DATETIME: "TIMESTAMP",
    exp.DataType.Type.DATE: "TIMESTAMP",
    exp.DataType.Type.TIME: "TIMESTAMP",
    exp.DataType.Type.TIMETZ: "TIMESTAMP",
    exp.DataType.Type.TIME_NS: "TIMESTAMP",
}


def _normalize_type(data_type: exp.DataType | None) -> str:
    """Map a sqlglot DataType node to a normalized type string."""
    if data_type is None:
        return "TEXT"
    return _TYPE_MAP.get(data_type.this, "TEXT")


# ── Column extraction ────────────────────────────────────────────────────────

def _parse_column(col_def: exp.ColumnDef) -> Column:
    """Extract a Column from a ColumnDef AST node."""
    name = col_def.name

    data_type = col_def.args.get("kind")
    col_type = _normalize_type(data_type)

    # NOT NULL — either explicit or implied by PRIMARY KEY
    not_null_constraint = col_def.find(exp.NotNullColumnConstraint)
    pk_inline = col_def.find(exp.PrimaryKeyColumnConstraint)
    nullable = not bool(not_null_constraint or pk_inline)

    # CHECK constraint (inline only — out-of-line CHECK not common in Flyway)
    check_constraint = col_def.find(exp.CheckColumnConstraint)
    check_expr = str(check_constraint.this) if check_constraint else None

    # DEFAULT value
    default_constraint = col_def.find(exp.DefaultColumnConstraint)
    default = str(default_constraint.this) if default_constraint else None

    return Column(
        name=name,
        col_type=col_type,
        nullable=nullable,
        primary_key=bool(pk_inline),
        check_expr=check_expr,
        default=default,
    )


# ── Primary key extraction ───────────────────────────────────────────────────

def _extract_primary_key_columns(stmt: exp.Create) -> list[str]:
    """
    Collect all primary key column names from a CREATE TABLE statement.
    Handles both inline (per-column) and out-of-line (table-level) forms.

    Inline:     account_id INTEGER PRIMARY KEY
    Out-of-line: PRIMARY KEY (account_id, name)
    """
    pk_columns: list[str] = []

    # Inline PKs — already set on Column.primary_key, but we also collect names here
    for col_def in stmt.find_all(exp.ColumnDef):
        if col_def.find(exp.PrimaryKeyColumnConstraint):
            pk_columns.append(col_def.name)

    # Out-of-line PKs — table-level PRIMARY KEY (...) constraint
    for pk in stmt.find_all(exp.PrimaryKey):
        # Avoid double-counting if somehow nested inside a ColumnDef
        parent = pk.parent
        if isinstance(parent, exp.ColumnDef):
            continue
        for identifier in pk.expressions:
            name = identifier.name
            if name and name not in pk_columns:
                pk_columns.append(name)

    return pk_columns


# ── Foreign key extraction ───────────────────────────────────────────────────

def _extract_foreign_keys(stmt: exp.Create) -> list[ForeignKey]:
    """
    Collect all foreign keys from a CREATE TABLE statement.
    Handles both inline and out-of-line forms.

    Inline:     account_id INTEGER REFERENCES accounts(account_id)
    Out-of-line: FOREIGN KEY (account_id) REFERENCES accounts(account_id)
    """
    foreign_keys: list[ForeignKey] = []

    # Out-of-line: FOREIGN KEY (...) REFERENCES ...
    for fk_node in stmt.find_all(exp.ForeignKey):
        source_cols = [ident.name for ident in fk_node.expressions]

        reference = fk_node.args.get("reference")
        if reference is None:
            continue

        ref_table_node = reference.find(exp.Table)
        ref_table = ref_table_node.name if ref_table_node else ""

        # Reference columns: REFERENCES accounts(account_id)
        # sqlglot puts these in reference.expressions or in a Schema node
        ref_cols: list[str] = []
        schema_node = reference.find(exp.Schema)
        if schema_node:
            ref_cols = [c.name for c in schema_node.find_all(exp.Column)]

        foreign_keys.append(ForeignKey(
            columns=source_cols,
            references_table=ref_table,
            references_columns=ref_cols,
        ))

    # Inline: account_id INTEGER REFERENCES accounts(account_id)
    for col_def in stmt.find_all(exp.ColumnDef):
        inline_ref = col_def.find(exp.Reference)
        if inline_ref is None:
            continue

        source_cols = [col_def.name]
        ref_table_node = inline_ref.find(exp.Table)
        ref_table = ref_table_node.name if ref_table_node else ""

        ref_cols = [c.name for c in inline_ref.find_all(exp.Column)]

        # Avoid duplicating if sqlglot already surfaced this as a ForeignKey node
        already_added = any(
            fk.columns == source_cols and fk.references_table == ref_table
            for fk in foreign_keys
        )
        if not already_added:
            foreign_keys.append(ForeignKey(
                columns=source_cols,
                references_table=ref_table,
                references_columns=ref_cols,
            ))

    return foreign_keys


# ── Table extraction ─────────────────────────────────────────────────────────

def _parse_create_table(stmt: exp.Create) -> Table:
    """Build a Table from a single CREATE TABLE AST node."""
    table_node = stmt.find(exp.Table)
    table_name = table_node.name if table_node else stmt.this.name

    columns = [_parse_column(col_def) for col_def in stmt.find_all(exp.ColumnDef)]

    pk_columns = _extract_primary_key_columns(stmt)

    # Mark columns that are part of the PK (for out-of-line PKs)
    for col in columns:
        if col.name in pk_columns:
            col.primary_key = True
            col.nullable = False    # PK columns are always NOT NULL

    foreign_keys = _extract_foreign_keys(stmt)

    return Table(
        name=table_name,
        columns=columns,
        primary_key_columns=pk_columns,
        foreign_keys=foreign_keys,
    )


# ── ALTER TABLE folding ──────────────────────────────────────────────────────
# Folds a single ALTER TABLE statement into an already-built SchemaModel, in
# the order it appears — later ALTERs see the effect of earlier ones, matching
# how Flyway applies a linear migration history. Anything not handled below
# raises ValueError (fail-closed) rather than being silently dropped.

def _fold_alter_table(schema: SchemaModel, stmt: exp.Alter) -> None:
    table_name = stmt.this.name
    table = schema.get_table(table_name)
    if table is None:
        raise ValueError(
            f"ALTER TABLE '{table_name}' references a table that doesn't exist yet "
            "(or was renamed away) at this point in the DDL. CREATE TABLE and "
            "ALTER TABLE statements must appear in migration order."
        )

    for action in stmt.args.get("actions") or []:
        if isinstance(action, exp.ColumnDef):
            _fold_add_column(table, action)
        elif isinstance(action, exp.AlterColumn):
            _fold_alter_column(table, action)
        elif isinstance(action, exp.AddConstraint):
            _fold_add_constraint(table, action)
        elif isinstance(action, exp.Drop):
            _fold_drop_action(table, action)
        elif isinstance(action, exp.RenameColumn):
            _fold_rename_column(table, action)
        elif isinstance(action, exp.AlterRename):
            _fold_rename_table(schema, table, action)
        else:
            raise ValueError(
                f"ALTER TABLE '{table.name}': unsupported action "
                f"{type(action).__name__!r}. Supported: ADD COLUMN, ALTER COLUMN "
                "SET/DROP NOT NULL, ADD CONSTRAINT (FOREIGN KEY / PRIMARY KEY / "
                "CHECK / UNIQUE), DROP COLUMN, RENAME TO, RENAME COLUMN."
            )


def _fold_add_column(table: Table, action: exp.ColumnDef) -> None:
    col = _parse_column(action)
    if table.get_column(col.name) is not None:
        raise ValueError(
            f"ALTER TABLE '{table.name}' ADD COLUMN '{col.name}': column already exists."
        )
    table.columns.append(col)


def _fold_alter_column(table: Table, action: exp.AlterColumn) -> None:
    name = action.this.name
    col = table.get_column(name)
    if col is None:
        raise ValueError(f"ALTER TABLE '{table.name}' ALTER COLUMN '{name}': no such column.")

    if action.args.get("dtype") is not None:
        raise ValueError(
            f"ALTER TABLE '{table.name}' ALTER COLUMN '{name}' TYPE ...: changing a "
            "column's type is not supported."
        )
    if action.args.get("collate") or action.args.get("using"):
        raise ValueError(
            f"ALTER TABLE '{table.name}' ALTER COLUMN '{name}': COLLATE/USING clauses "
            "are not supported."
        )

    allow_null = action.args.get("allow_null")
    if allow_null is None:
        raise ValueError(f"ALTER TABLE '{table.name}' ALTER COLUMN '{name}': unsupported form.")
    if col.primary_key and allow_null:
        raise ValueError(
            f"ALTER TABLE '{table.name}' ALTER COLUMN '{name}' DROP NOT NULL: "
            f"'{name}' is a primary key column and cannot be made nullable."
        )
    col.nullable = allow_null


def _fold_add_constraint(table: Table, action: exp.AddConstraint) -> None:
    # Reused verbatim: these helpers only look for FK/PK nodes anywhere under
    # the given node via find_all, so they work on an AddConstraint action
    # exactly as they do on a whole CREATE TABLE statement.
    new_fks = _extract_foreign_keys(action)
    new_pks = _extract_primary_key_columns(action)

    if not new_fks and not new_pks:
        # CHECK / UNIQUE — parsed but not encoded into Z3, same as an inline
        # constraint on CREATE TABLE (see CLAUDE.md "Known limitations").
        if action.find(exp.CheckColumnConstraint) or action.find(exp.UniqueColumnConstraint):
            return
        raise ValueError(
            f"ALTER TABLE '{table.name}' ADD CONSTRAINT: unsupported constraint form. "
            "Supported: FOREIGN KEY, PRIMARY KEY, CHECK, UNIQUE."
        )

    table.foreign_keys.extend(new_fks)
    for pk_name in new_pks:
        col = table.get_column(pk_name)
        if col is None:
            raise ValueError(
                f"ALTER TABLE '{table.name}' ADD CONSTRAINT PRIMARY KEY: "
                f"no such column '{pk_name}'."
            )
        col.primary_key = True
        col.nullable = False
        if pk_name not in table.primary_key_columns:
            table.primary_key_columns.append(pk_name)


def _fold_drop_action(table: Table, action: exp.Drop) -> None:
    kind = action.args.get("kind")
    if kind == "COLUMN":
        name = action.this.name
        if table.get_column(name) is None:
            raise ValueError(f"ALTER TABLE '{table.name}' DROP COLUMN '{name}': no such column.")
        table.columns = [c for c in table.columns if c.name != name]
        if name in table.primary_key_columns:
            table.primary_key_columns.remove(name)
        table.foreign_keys = [fk for fk in table.foreign_keys if name not in fk.columns]
        return
    if kind == "CONSTRAINT":
        raise ValueError(
            f"ALTER TABLE '{table.name}' DROP CONSTRAINT: not supported — Skolem "
            "doesn't track constraint names, so it can't identify which constraint "
            "to remove."
        )
    raise ValueError(f"ALTER TABLE '{table.name}' DROP {kind}: not supported.")


def _fold_rename_column(table: Table, action: exp.RenameColumn) -> None:
    old_name = action.this.name
    new_name = action.args["to"].name
    col = table.get_column(old_name)
    if col is None:
        raise ValueError(
            f"ALTER TABLE '{table.name}' RENAME COLUMN '{old_name}': no such column."
        )
    col.name = new_name
    if old_name in table.primary_key_columns:
        table.primary_key_columns = [
            new_name if c == old_name else c for c in table.primary_key_columns
        ]
    for fk in table.foreign_keys:
        fk.columns = [new_name if c == old_name else c for c in fk.columns]


def _fold_rename_table(schema: SchemaModel, table: Table, action: exp.AlterRename) -> None:
    new_name = action.this.name
    if new_name in schema.tables and schema.tables[new_name] is not table:
        raise ValueError(
            f"ALTER TABLE '{table.name}' RENAME TO '{new_name}': a table named "
            f"'{new_name}' already exists."
        )
    del schema.tables[table.name]
    table.name = new_name
    schema.tables[new_name] = table


# ── Public API ───────────────────────────────────────────────────────────────

def parse_ddl(ddl_sql: str, dialect: str = "generic") -> SchemaModel:
    """
    Parse a DDL string (one or more CREATE TABLE statements) into a SchemaModel.

    Args:
        ddl_sql:  Raw SQL text from a Flyway migration file.
        dialect:  SQL dialect for parsing. Common values:
                  'postgres', 'mysql', 'sqlite', 'tsql', 'bigquery', 'snowflake'
                  Defaults to 'generic' which handles standard SQL.

    Returns:
        SchemaModel with all tables, columns, and constraints populated.

    Raises:
        ValueError: If ddl_sql is empty, contains no CREATE TABLE statements,
            or contains a statement outside the supported subset (fail-closed
            — see the module docstring for exactly what's handled).
    """
    if not ddl_sql or not ddl_sql.strip():
        raise ValueError("DDL input is empty.")

    # sqlglot uses None for generic/standard SQL, not the string 'generic'
    sqlglot_dialect = None if dialect == "generic" else dialect
    statements = sqlglot.parse(ddl_sql, dialect=sqlglot_dialect)
    schema = SchemaModel(dialect=dialect)

    for stmt in statements:
        if isinstance(stmt, exp.Create):
            kind = stmt.args.get("kind")
            if kind == "INDEX":
                continue  # semantics-neutral — never changes query results
            if kind == "TABLE" and isinstance(stmt.this, exp.Schema):
                table = _parse_create_table(stmt)
                schema.tables[table.name] = table
                continue
            raise ValueError(
                f"Unsupported DDL statement: CREATE {kind or type(stmt.this).__name__}. "
                "Only CREATE TABLE and CREATE INDEX are supported."
            )
        elif isinstance(stmt, exp.Alter):
            if stmt.args.get("kind") != "TABLE":
                raise ValueError(
                    f"Unsupported DDL statement: ALTER {stmt.args.get('kind')}. "
                    "Only ALTER TABLE is supported."
                )
            _fold_alter_table(schema, stmt)
        elif stmt is not None:
            raise ValueError(
                f"Unsupported DDL statement: {type(stmt).__name__}. Only CREATE TABLE, "
                "CREATE INDEX, and ALTER TABLE are supported — Skolem never silently "
                "drops a schema-defining statement."
            )

    if not schema.tables:
        raise ValueError(
            "No CREATE TABLE statements found in DDL input. "
            "Make sure the file contains valid table definitions."
        )

    return schema