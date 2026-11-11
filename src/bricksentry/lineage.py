"""Downstream impact ("blast radius").

``UnityCatalogLineage`` reads Databricks lineage system tables, which are filled
at *runtime* from query plans. That means it sees PySpark notebooks, Jobs,
Lakeflow pipelines, dashboards and SQL queries, not just SQL you can parse.

``StaticLineage`` takes edges from config, for local work, tests and non-UC estates.
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from typing import Any

from bricksentry.backends.base import Backend
from bricksentry.models import ColumnImpact, ImpactNode

_ENTITY_KINDS = {
    "NOTEBOOK": "notebook",
    "JOB": "job",
    "PIPELINE": "pipeline",
    "DLT_PIPELINE": "pipeline",
    "DASHBOARD": "dashboard",
    "DASHBOARD_V3": "dashboard",
    "DBSQL_DASHBOARD": "dashboard",
    "DBSQL_QUERY": "query",
    "SQL_QUERY": "query",
    "GENIE_SPACE": "genie_space",
}


class LineageProvider(ABC):
    @abstractmethod
    def downstream(self, table: str, depth: int = 3) -> list[ImpactNode]: ...

    def owner_and_tags(self, table: str) -> tuple[str | None, dict[str, str]]:
        return None, {}

    def column_tags(self, table: str) -> dict[str, dict[str, str]]:
        """Tags on the table's columns: {column: {tag: value}}. Empty if unknown."""
        return {}

    def column_downstream(self, table: str, columns: list[str], depth: int = 2) -> list[ColumnImpact]:
        """Downstream columns fed by ``columns`` of ``table``, with their tags."""
        return []


class StaticLineage(LineageProvider):
    """Lineage from config::

        lineage:
          type: static
          edges:
            main.sales.orders: [main.sales.daily_revenue, "dashboard:Revenue Overview"]
          owners:
            main.sales.daily_revenue: finance-team
          tags:
            main.sales.daily_revenue: {certified: "true"}
          column_edges:            # "table.column" -> downstream "table.column"s
            main.sales.orders.amount: [main.sales.daily_revenue.revenue]
          column_tags:             # table -> column -> tags
            main.sales.orders: {email: {pii: ""}}
    """

    def __init__(
        self,
        edges: dict[str, list[str]] | None = None,
        owners: dict | None = None,
        tags: dict | None = None,
        column_edges: dict[str, list[str]] | None = None,
        column_tags: dict | None = None,
    ):
        self.edges = edges or {}
        self.owners = owners or {}
        self.tags = tags or {}
        self.column_edges = {k.lower(): v for k, v in (column_edges or {}).items()}
        self.col_tags = {t.lower(): v for t, v in (column_tags or {}).items()}

    def downstream(self, table: str, depth: int = 3) -> list[ImpactNode]:
        out: list[ImpactNode] = []
        seen = {table}
        frontier = [table]
        for d in range(1, depth + 1):
            nxt = []
            for src in frontier:
                for tgt in self.edges.get(src, []):
                    kind, name = _parse_static(tgt)
                    if name in seen:
                        continue
                    seen.add(name)
                    out.append(
                        ImpactNode(
                            kind=kind,
                            name=name,
                            depth=d,
                            via=src,
                            owner=self.owners.get(name),
                            tags={str(k): str(v) for k, v in (self.tags.get(name) or {}).items()},
                        )
                    )
                    if kind == "table":
                        nxt.append(name)
            frontier = nxt
            if not frontier:
                break
        return out

    def owner_and_tags(self, table: str) -> tuple[str | None, dict[str, str]]:
        return self.owners.get(table), {str(k): str(v) for k, v in (self.tags.get(table) or {}).items()}

    def column_tags(self, table: str) -> dict[str, dict[str, str]]:
        cols = self.col_tags.get(table.lower()) or {}
        return {str(c): {str(k): "" if v is None else str(v) for k, v in (t or {}).items()} for c, t in cols.items()}

    def column_downstream(self, table: str, columns: list[str], depth: int = 2) -> list[ColumnImpact]:
        out: list[ColumnImpact] = []
        seen: set[str] = set()
        frontier = [(c, f"{table}.{c}") for c in columns]
        for d in range(1, depth + 1):
            nxt = []
            for source_col, ref in frontier:
                for tgt in self.column_edges.get(ref.lower(), []):
                    if tgt.lower() in seen:
                        continue
                    seen.add(tgt.lower())
                    t_table, _, t_col = tgt.rpartition(".")
                    out.append(ColumnImpact(source_col, t_table, t_col, d, self.column_tags(t_table).get(t_col, {})))
                    nxt.append((source_col, tgt))
            frontier = nxt
        return out


def _parse_static(s: str) -> tuple[str, str]:
    if ":" in s and not s.startswith(("http:", "https:")):
        kind, name = s.split(":", 1)
        return kind.strip().lower(), name.strip()
    return "table", s


class UnityCatalogLineage(LineageProvider):
    """Breadth-first walk over ``system.access.table_lineage``.

    One query per depth level (not per node), bounded by ``lookback_days``.
    Table owners come from ``information_schema.tables`` and tags from
    ``information_schema.table_tags``. All table names are configurable so the
    same code runs against copies of the system tables in tests.

    Lineage history keeps tables that no longer exist, including bricksentry's own
    ``preview_write`` clones. Those are left out of the blast radius: names matching
    ``exclude_patterns`` always, and tables that ``information_schema`` reports as
    gone (only when that lookup succeeds, so missing privileges never hide anything).
    """

    def __init__(
        self,
        backend: Backend,
        lookback_days: int = 90,
        lineage_table: str = "system.access.table_lineage",
        tables_table: str = "system.information_schema.tables",
        tags_table: str = "system.information_schema.table_tags",
        max_nodes: int = 500,
        exclude_patterns: list[str] | None = None,
        exclude_dropped: bool = True,
        column_lineage_table: str = "system.access.column_lineage",
        column_tags_table: str = "system.information_schema.column_tags",
    ):
        self.backend = backend
        self.lookback_days = lookback_days
        self.lineage_table = lineage_table
        self.tables_table = tables_table
        self.tags_table = tags_table
        self.max_nodes = max_nodes
        self.exclude_patterns = [p.lower() for p in (exclude_patterns if exclude_patterns is not None else DEFAULT_EXCLUDE)]
        self.exclude_dropped = exclude_dropped
        self.column_lineage_table = column_lineage_table
        self.column_tags_table = column_tags_table
        self._col_tag_cache: dict[str, dict[str, dict[str, str]]] = {}
        self._meta_cache: dict[str, tuple[str | None, dict[str, str]]] = {}
        self._exists: dict[str, bool] = {}  # True / False when information_schema answered; absent if unknown

    def _excluded(self, name: str) -> bool:
        n = name.lower()
        return any(fnmatch.fnmatch(n, p) for p in self.exclude_patterns)

    def downstream(self, table: str, depth: int = 3) -> list[ImpactNode]:
        out: list[ImpactNode] = []
        seen_tables = {table.lower()}
        seen_entities: set[tuple[str, str]] = set()
        frontier = [table]
        for d in range(1, depth + 1):
            if not frontier or len(out) >= self.max_nodes:
                break
            in_list = ", ".join(_lit(t) for t in frontier)
            sql = (
                "SELECT DISTINCT source_table_full_name AS src, target_table_full_name AS tgt, "
                "entity_type, CAST(entity_id AS STRING) AS entity_id "
                f"FROM {self.lineage_table} "
                f"WHERE lower(source_table_full_name) IN ({in_list.lower()}) "
                f"AND event_date >= {self.backend.days_ago(self.lookback_days)}"
            )
            rows = self.backend.query(sql)
            nxt: list[str] = []
            for r in rows:
                src = r.get("src")
                tgt = r.get("tgt")
                if tgt and self._excluded(tgt):
                    continue  # e.g. a preview_write clone: neither it nor the statement that made it is a dependent
                if tgt and tgt.lower() not in seen_tables:
                    seen_tables.add(tgt.lower())
                    nxt.append(tgt)
                    out.append(ImpactNode(kind="table", name=tgt, depth=d, via=src))
                et = (r.get("entity_type") or "").upper()
                eid = r.get("entity_id")
                if et and eid and (et, eid) not in seen_entities:
                    seen_entities.add((et, eid))
                    out.append(
                        ImpactNode(kind=_ENTITY_KINDS.get(et, et.lower()), name=f"{et.lower()}:{eid}", depth=d, via=src)
                    )
            frontier = nxt
        out = out[: self.max_nodes]
        self._attach_meta([n for n in out if n.kind == "table"])
        if self.exclude_dropped:
            out = [n for n in out if not (n.kind == "table" and self._exists.get(n.name.lower()) is False)]
        return out

    def owner_and_tags(self, table: str) -> tuple[str | None, dict[str, str]]:
        if table.lower() not in self._meta_cache:
            self._load_meta([table])
        return self._meta_cache.get(table.lower(), (None, {}))

    def _attach_meta(self, nodes: list[ImpactNode]) -> None:
        names = [n.name for n in nodes if n.name.lower() not in self._meta_cache]
        if names:
            self._load_meta(names)
        for n in nodes:
            n.owner, n.tags = self._meta_cache.get(n.name.lower(), (None, {}))

    def _load_meta(self, names: list[str]) -> None:
        for name in names:
            self._meta_cache.setdefault(name.lower(), (None, {}))
        full = "lower(concat_ws('.', table_catalog, table_schema, table_name))"
        tag_full = "lower(concat_ws('.', catalog_name, schema_name, table_name))"
        for i in range(0, len(names), 200):
            batch = names[i : i + 200]
            in_list = ", ".join(_lit(n.lower()) for n in batch)
            try:
                found = set()
                for r in self.backend.query(
                    f"SELECT {full} AS fqn, table_owner FROM {self.tables_table} WHERE {full} IN ({in_list})"
                ):
                    found.add(r["fqn"])
                    _, tags = self._meta_cache[r["fqn"]]
                    self._meta_cache[r["fqn"]] = (r.get("table_owner"), tags)
                for n in batch:
                    self._exists[n.lower()] = n.lower() in found
            except Exception:
                pass  # missing privileges on information_schema should not break the check
            try:
                for r in self.backend.query(
                    f"SELECT {tag_full} AS fqn, tag_name, tag_value FROM {self.tags_table} "
                    f"WHERE {tag_full} IN ({in_list})"
                ):
                    owner, tags = self._meta_cache[r["fqn"]]
                    tags = dict(tags)
                    tags[str(r["tag_name"])] = "" if r.get("tag_value") is None else str(r["tag_value"])
                    self._meta_cache[r["fqn"]] = (owner, tags)
            except Exception:
                pass


    # --- column level -------------------------------------------------------------
    def column_tags(self, table: str) -> dict[str, dict[str, str]]:
        if table.lower() not in self._col_tag_cache:
            self._load_column_tags([table])
        return self._col_tag_cache.get(table.lower(), {})

    def _load_column_tags(self, tables: list[str]) -> None:
        """Raises if information_schema can't be read; callers decide how to degrade."""
        for t in tables:
            self._col_tag_cache.setdefault(t.lower(), {})
        full = "lower(concat_ws('.', catalog_name, schema_name, table_name))"
        for i in range(0, len(tables), 200):
            in_list = ", ".join(_lit(t.lower()) for t in tables[i : i + 200])
            for r in self.backend.query(
                f"SELECT {full} AS fqn, column_name, tag_name, tag_value FROM {self.column_tags_table} "
                f"WHERE {full} IN ({in_list})"
            ):
                cols = self._col_tag_cache.setdefault(r["fqn"], {})
                cols.setdefault(str(r["column_name"]), {})[str(r["tag_name"])] = (
                    "" if r.get("tag_value") is None else str(r["tag_value"])
                )

    def column_downstream(self, table: str, columns: list[str], depth: int = 2) -> list[ColumnImpact]:
        """Breadth-first over ``system.access.column_lineage``, starting from the changed
        columns only, one query per level. Preview clones and (when ``exclude_dropped``)
        dropped tables are left out, as in ``downstream``. Raises if the lineage table
        can't be read."""
        out: list[ColumnImpact] = []
        origin: dict[str, str] = {f"{table}.{c}".lower(): c for c in columns}
        seen = set(origin)
        frontier = list(origin)
        src = "lower(concat_ws('.', source_table_full_name, source_column_name))"
        for d in range(1, depth + 1):
            if not frontier or len(out) >= self.max_nodes:
                break
            in_list = ", ".join(_lit(f) for f in frontier)
            rows = self.backend.query(
                f"SELECT DISTINCT {src} AS src, target_table_full_name AS tgt_table, target_column_name AS tgt_col "
                f"FROM {self.column_lineage_table} WHERE {src} IN ({in_list}) "
                f"AND target_table_full_name IS NOT NULL AND target_column_name IS NOT NULL "
                f"AND event_date >= {self.backend.days_ago(self.lookback_days)}"
            )
            nxt = []
            for r in rows:
                t_table, t_col = str(r["tgt_table"]), str(r["tgt_col"])
                key = f"{t_table}.{t_col}".lower()
                if key in seen or self._excluded(t_table):
                    continue
                seen.add(key)
                origin[key] = origin.get(str(r["src"]).lower(), str(r["src"]).rpartition(".")[2])
                out.append(ColumnImpact(origin[key], t_table, t_col, d))
                nxt.append(key)
            frontier = nxt
        out = out[: self.max_nodes]
        if self.exclude_dropped and out:
            # Same rule as the table-level blast radius: lineage history keeps tables that
            # were dropped since. Leave those out, but only when information_schema answered.
            missing = sorted({c.table for c in out if c.table.lower() not in self._meta_cache})
            if missing:
                self._load_meta(missing)
            out = [c for c in out if self._exists.get(c.table.lower()) is not False]
        tables = sorted({c.table for c in out})
        if tables:
            try:
                self._load_column_tags(tables)
            except Exception:
                pass  # tags are extra detail; the lineage itself stands
            for c in out:
                c.tags = self._col_tag_cache.get(c.table.lower(), {}).get(c.column, {})
        return out


DEFAULT_EXCLUDE = ["*__preview_*"]  # bricksentry's own preview_write clones


def _lit(s: str) -> str:
    s = str(s)
    if any(ch in s for ch in "'\\\n\r;"):
        # Table names can come from agents; refuse anything that could break out of a literal.
        raise ValueError(f"Refusing suspicious table name in lineage lookup: {s!r}")
    return "'" + s + "'"


def make_lineage(cfg: dict[str, Any] | None, backend: Backend) -> LineageProvider:
    cfg = dict(cfg or {})
    kind = cfg.get("type") or ("databricks" if backend.name == "databricks" else "static")
    if kind == "static":
        return StaticLineage(
            cfg.get("edges"), cfg.get("owners"), cfg.get("tags"), cfg.get("column_edges"), cfg.get("column_tags")
        )
    if kind in ("databricks", "unity_catalog", "uc"):
        return UnityCatalogLineage(
            backend,
            lookback_days=int(cfg.get("lookback_days", 90)),
            lineage_table=cfg.get("lineage_table", "system.access.table_lineage"),
            tables_table=cfg.get("tables_table", "system.information_schema.tables"),
            tags_table=cfg.get("tags_table", "system.information_schema.table_tags"),
            max_nodes=int(cfg.get("max_nodes", 500)),
            exclude_patterns=cfg.get("exclude"),
            exclude_dropped=bool(cfg.get("exclude_dropped", True)),
            column_lineage_table=cfg.get("column_lineage_table", "system.access.column_lineage"),
            column_tags_table=cfg.get("column_tags_table", "system.information_schema.column_tags"),
        )
    raise ValueError(f"Unknown lineage type {kind!r}")
