"""Backend interface. All heavy lifting is pushed down as SQL to the engine
(Databricks SQL warehouse, or DuckDB locally), so bricksentry itself needs no Spark
session and stays cheap enough for an agent to call in a loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

NUMERIC_PREFIXES = (
    "tinyint", "smallint", "int", "bigint", "long", "short", "byte", "integer",
    "float", "double", "real", "decimal", "numeric", "hugeint", "ubigint", "uinteger",
    "usmallint", "utinyint",
)


class Backend(ABC):
    name: str = "base"
    supports_time_travel: bool = False

    # --- execution ---------------------------------------------------------
    @abstractmethod
    def query(self, sql: str) -> list[dict[str, Any]]:
        """Run SQL and return rows as dicts."""

    def execute(self, sql: str) -> None:
        self.query(sql)

    def scalar(self, sql: str) -> Any:
        rows = self.query(sql)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    # --- dialect -----------------------------------------------------------
    @abstractmethod
    def quote(self, ident: str) -> str: ...

    def qualified(self, table: str) -> str:
        return ".".join(self.quote(p) for p in table.split("."))

    @property
    def string_type(self) -> str:
        return "STRING"

    @abstractmethod
    def sample_predicate(self, keys: list[str], pct: float, alias: str | None = None) -> str:
        """Deterministic hash sample on the key columns, identical on both sides."""

    def row_checksum(self, columns: list[str]) -> str | None:
        """SQL aggregate: an order-independent checksum over ``columns`` for every row.

        Used to check a sampled diff against the whole table. ``None`` means the
        backend has no cheap row hash, and the guard is skipped."""
        return None

    @abstractmethod
    def days_ago(self, days: int) -> str:
        """SQL expression for the date ``days`` days ago."""

    # --- metadata ----------------------------------------------------------
    @abstractmethod
    def columns(self, table: str) -> dict[str, str]:
        """Ordered mapping column -> lower-cased type name."""

    def table_exists(self, table: str) -> bool:
        try:
            self.columns(table)
            return True
        except Exception:
            return False

    @abstractmethod
    def clone(self, source: str, target: str) -> None:
        """Make a cheap copy of ``source`` at ``target`` (Delta SHALLOW CLONE on Databricks)."""

    def drop(self, table: str) -> None:
        self.execute(f"DROP TABLE IF EXISTS {self.qualified(table)}")

    def ensure_schema(self, schema: str) -> None:
        self.execute(f"CREATE SCHEMA IF NOT EXISTS {self.qualified(schema)}")

    def current_version(self, table: str) -> int | None:
        return None

    @staticmethod
    def is_numeric(type_name: str) -> bool:
        t = type_name.lower()
        return t.startswith(NUMERIC_PREFIXES)


def make_backend(spec: dict[str, Any] | None) -> Backend:
    spec = dict(spec or {"type": "duckdb"})
    kind = spec.pop("type", "duckdb")
    if kind == "duckdb":
        from bricksentry.backends.duckdb_backend import DuckDBBackend

        return DuckDBBackend(**spec)
    if kind == "databricks":
        from bricksentry.backends.databricks_backend import DatabricksBackend

        return DatabricksBackend(**spec)
    raise ValueError(f"Unknown backend type: {kind!r} (expected 'databricks' or 'duckdb')")
