"""
core/models.py

Pure dataclasses - no FastAPI, no DB, no Z3 imports.
Everything in core/ depends on these shapes.
"""

from dataclasses import dataclass, field
from typing import Optional


# Schema layer

@dataclass
class Column:
    name: str
    col_type: str               # normalized: INTEGER | TEXT | REAL | BOOLEAN | TIMESTAMP
    nullable: bool = True
    primary_key: bool = False
    check_expr: Optional[str] = None
    default: Optional[str] = None


@dataclass
class ForeignKey:
    columns: list[str]
    references_table: str
    references_columns: list[str]


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key_columns: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def get_column(self, name: str) -> Optional[Column]:
        for col in self.columns:
            if col.name == name:
                return col
        return None


@dataclass
class SchemaModel:
    tables: dict[str, Table] = field(default_factory=dict)
    dialect: str = "generic"

    def get_table(self, name: str) -> Optional[Table]:
        return self.tables.get(name)


# Verification layer

@dataclass
class VerificationResult:
    status: str                              # 'equivalent' | 'divergent' | 'unknown' | 'error'
    counterexample_db: Optional[dict] = None # {table_name: [row_dict, ...]} synthetic DB
    query_v1_output: Optional[list] = None   # rows v1 returns on counterexample DB
    query_v2_output: Optional[list] = None   # rows v2 returns on counterexample DB
    divergence_reason: Optional[str] = None  # e.g. "row presence differs"
    error_message: Optional[str] = None      # populated when status == 'error'
    explanation: Optional[str] = None        # populated by explainer/ only, never here

    @property
    def is_safe(self) -> bool:
        return self.status == "equivalent"