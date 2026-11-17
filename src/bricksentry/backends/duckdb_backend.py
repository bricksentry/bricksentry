"""DuckDB backend: for local development, tests, demos and offline CI."""

from __future__ import annotations

from typing import Any

from bricksentry.backends.base import Backend


class DuckDBBackend(Backend):
    name = "duckdb"
    supports_time_travel = False

    def __init__(self, database: str = ":memory:", connection: Any = None, **_: Any):
        if connection is None:
            try:
                import duckdb
            except ImportError as e:  # pragma: no cover
                raise ImportError("DuckDB backend needs `pip install bricksentry[duckdb]`") from e
            connection = duckdb.connect(database)
        self.con = connection

    def query(self, sql: str) -> list[dict[str, Any]]:
        cur = self.con.execute(sql)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    @property
    def string_type(self) -> str:
        return "VARCHAR"

    def row_checksum(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        return f"SUM(CAST({self.row_hash(columns, types)} AS HUGEINT))"

    def row_hash(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        types = types or {}
        return f"hash({', '.join(self.canon(self.quote(c), types.get(c, '')) for c in columns)})"

    def canon(self, expr: str, type_name: str) -> str:
        # Maps compare in insertion order; compare their entries sorted by key instead.
        t = (type_name or "").lower()
        if t.startswith("map("):
            return f"list_sort(map_entries({expr}))"
        if "map(" in t:
            return f"CAST({expr} AS VARCHAR)"
        return expr

    def sample_predicate(self, keys: list[str], pct: float, alias: str | None = None) -> str:
        pre = f"{alias}." if alias else ""
        cols = ", ".join(f"{pre}{self.quote(k)}" for k in keys)
        return f"(hash({cols}) % 10000) < {int(pct * 100)}"

    def days_ago(self, days: int) -> str:
        return f"(current_date - INTERVAL {int(days)} DAY)"

    def columns(self, table: str) -> dict[str, str]:
        rows = self.query(f"DESCRIBE {self.qualified(table)}")
        return {r["column_name"]: str(r["column_type"]).lower() for r in rows}

    def clone(self, source: str, target: str) -> None:
        self.execute(
            f"CREATE OR REPLACE TABLE {self.qualified(target)} AS SELECT * FROM {self.qualified(source)}"
        )
