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

    def view_definitions(self, tables: list[str]) -> dict[str, tuple[str | None, str | None, str | None]]:
        """For the names that are views: ``{name: (definition SQL, catalog, schema)}``.
        Tables aren't listed. Raises if the catalog can't be read."""
        return {}

    def function_bodies(self, names: list[str]) -> list[tuple[str, str | None, str, str | None, str | None]]:
        """User functions matching ``names`` (an unqualified name matches any schema):
        ``[(full name, body, language, catalog, schema)]``. Built-ins aren't listed."""
        return []

    def plan_tables(self, sql: str) -> list[str] | None:
        """Every table the engine's own query plan for ``sql`` reads, without running it.
        ``None`` when the engine offers no plan to check (DuckDB)."""
        return None

    def protections(self, tables: list[str]) -> dict[str, dict[str, Any]]:
        """Column masks and row filters per table: ``{table: {"masked": [cols], "row_filter": bool}}``.
        Engines without them return nothing."""
        return {}

    def columns_at(self, table: str, version: int | None, strict: bool = False) -> dict[str, str]:
        """Like :meth:`columns`, for ``table`` at ``version``. With ``strict``, raise if the
        version can't be read (engines without time travel just read the table)."""
        return self.columns(table)

    def canon(self, expr: str, type_name: str) -> str:
        """An expression for ``expr`` that can be compared and hashed (e.g. a MAP as
        key-sorted JSON on engines that can't compare maps). Identity by default."""
        return expr

    def at_version(self, table: str, version: int | None) -> str:
        """``table`` as it was at ``version`` (for a FROM clause). Engines without
        time travel read the table as it is."""
        return self.qualified(table)

    @property
    def string_type(self) -> str:
        return "STRING"

    @abstractmethod
    def sample_predicate(self, keys: list[str], pct: float, alias: str | None = None) -> str:
        """Deterministic hash sample on the key columns, identical on both sides."""

    def row_checksum(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        """SQL aggregate: an order-independent checksum over ``columns`` for every row.

        Used to check a sampled diff against the whole table. ``None`` means the
        backend has no cheap row hash, and the guard is skipped."""
        return None

    def row_hash(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        """SQL for a hash of one row's ``columns``; orders copies of a duplicated key
        so identical copies pair up. ``None`` means unsupported (copies are paired in
        no particular order)."""
        return None

    def exact_sum(self, expr: str, type_name: str) -> str:
        """SUM that doesn't drift on large tables: decimals and integers are added as
        DECIMAL(38, x), only floating-point columns as DOUBLE."""
        t = type_name.lower()
        if t.startswith(("decimal", "numeric")):
            return f"SUM({expr})"
        if t.startswith(("float", "double", "real")):
            return f"SUM(CAST({expr} AS DOUBLE))"
        return f"SUM(CAST({expr} AS DECIMAL(38, 0)))"

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

    def history(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        """Table history rows (version, timestamp, operation, ...), newest first."""
        raise NotImplementedError(f"the {self.name} backend has no table history")

    def clone_version(self, source: str, target: str, version: int) -> None:
        """Cheap copy of ``source`` as it was at ``version``."""
        raise NotImplementedError(f"the {self.name} backend has no time travel")

    def primary_key(self, table: str) -> list[str]:
        """The table's PRIMARY KEY columns, or [] if none / unknown."""
        return []

    @staticmethod
    def is_numeric(type_name: str) -> bool:
        t = type_name.lower()
        return t.startswith(NUMERIC_PREFIXES)


def make_backend(spec: dict[str, Any] | None) -> Backend:
    spec = dict(spec or {"type": "duckdb"})
    kind = spec.pop("type", "duckdb")
    if kind == "duckdb":
        from bricksentry.backends.duckdb_backend import DuckDBBackend

        return DuckDBBackend(**_known_keys(DuckDBBackend, kind, spec))
    if kind == "databricks":
        from bricksentry.backends.databricks_backend import DatabricksBackend

        return DatabricksBackend(**_known_keys(DatabricksBackend, kind, spec))
    raise ValueError(f"Unknown backend type: {kind!r} (expected 'databricks' or 'duckdb')")



def _known_keys(cls: type, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Reject misspelled `backend:` keys instead of silently ignoring them (a `path:`
    meant as `database:` would quietly connect to an empty in-memory database)."""
    import inspect

    params = inspect.signature(cls.__init__).parameters
    known = [n for n, p in params.items() if n != "self" and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]
    unknown = sorted(set(spec) - set(known))
    if unknown:
        raise ValueError(f"Unknown key(s) under backend: {', '.join(unknown)} for type {kind}. "
                         f"Valid keys: type, {', '.join(k for k in known if k != 'client' and k != 'connection')}.")
    return spec
