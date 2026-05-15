"""
core/ddl_parser.py

Parses a Flyway-style DDL file (CREATE TABLE statements) into a SchemaModel.

Handles:
  - Inline and out-of-line PRIMARY KEY
  - Inline and out-of-line FOREIGN KEY
  - NOT NULL constraints
  - CHECK constraints (inline)
  - DEFAULT values
  - Multiple SQL dialects via sqlglot (postgres, mysql, sqlite, etc.)

Does NOT handle:
  - ALTER TABLE (Flyway V2__ migration files that alter existing tables)
  - CREATE INDEX
  - Anything other than CREATE TABLE

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
        ValueError: If ddl_sql is empty or contains no CREATE TABLE statements.
    """
    if not ddl_sql or not ddl_sql.strip():
        raise ValueError("DDL input is empty.")

    # sqlglot uses None for generic/standard SQL, not the string 'generic'
    sqlglot_dialect = None if dialect == "generic" else dialect
    statements = sqlglot.parse(ddl_sql, dialect=sqlglot_dialect)
    schema = SchemaModel(dialect=dialect)

    for stmt in statements:
        if not isinstance(stmt, exp.Create):
            continue
        # Only handle CREATE TABLE, not CREATE INDEX / CREATE VIEW
        if not isinstance(stmt.this, exp.Schema):
            continue

        table = _parse_create_table(stmt)
        schema.tables[table.name] = table

    if not schema.tables:
        raise ValueError(
            "No CREATE TABLE statements found in DDL input. "
            "Make sure the file contains valid table definitions."
        )

    return schema