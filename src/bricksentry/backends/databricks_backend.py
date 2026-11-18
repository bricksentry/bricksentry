"""Databricks backend: runs SQL on a SQL warehouse via the Statement Execution API.

Auth uses the standard Databricks SDK chain (env vars, ~/.databrickscfg profile,
OAuth, Azure/GCP identity...). Nothing in bricksentry handles credentials itself.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from bricksentry.backends.base import Backend

_INT_TYPES = {"BYTE", "SHORT", "INT", "LONG"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "DECIMAL"}


class BricksentryQueryError(RuntimeError):
    pass


class DatabricksBackend(Backend):
    name = "databricks"
    supports_time_travel = True

    def __init__(
        self,
        warehouse_id: str | None = None,
        profile: str | None = None,
        client: Any = None,
        timeout_s: int = 600,
        **_: Any,
    ):
        self.warehouse_id = warehouse_id or os.environ.get("DATABRICKS_WAREHOUSE_ID")
        if not self.warehouse_id:
            raise ValueError(
                "Databricks backend needs a SQL warehouse: set backend.warehouse_id in "
                "bricksentry.yml or the DATABRICKS_WAREHOUSE_ID env var."
            )
        if client is None:
            try:
                from databricks.sdk import WorkspaceClient
            except ImportError as e:  # pragma: no cover
                raise ImportError("Databricks backend needs `pip install bricksentry[databricks]`") from e
            try:
                client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
            except Exception as e:
                raise ValueError(_auth_help(e, profile)) from None
        self.client = client
        self.timeout_s = timeout_s

    # --- execution ---------------------------------------------------------
    def query(self, sql: str) -> list[dict[str, Any]]:
        api = self.client.statement_execution
        resp = api.execute_statement(
            statement=sql,
            warehouse_id=self.warehouse_id,
            wait_timeout="30s",
            **self._format_kwargs(),
        )
        deadline = time.monotonic() + self.timeout_s
        while _state(resp) in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                try:
                    api.cancel_execution(resp.statement_id)
                finally:
                    raise BricksentryQueryError(f"Query timed out after {self.timeout_s}s")
            time.sleep(1.0)
            resp = api.get_statement(resp.statement_id)

        state = _state(resp)
        if state != "SUCCEEDED":
            err = getattr(getattr(resp, "status", None), "error", None)
            msg = getattr(err, "message", None) or str(err) or state
            raise BricksentryQueryError(f"Databricks query {state}: {msg}\nSQL: {sql[:500]}")

        manifest = resp.manifest
        if manifest is None or manifest.schema is None or not manifest.schema.columns:
            return []
        cols = manifest.schema.columns
        names = [c.name for c in cols]
        types = [_type_name(c) for c in cols]

        rows: list[list[Any]] = []
        chunk = resp.result
        while chunk is not None:
            rows.extend(chunk.data_array or [])
            nxt = getattr(chunk, "next_chunk_index", None)
            if nxt is None:
                break
            chunk = api.get_statement_result_chunk_n(resp.statement_id, nxt)
        return [{n: _convert(v, t) for n, v, t in zip(names, row, types, strict=False)} for row in rows]

    @staticmethod
    def _format_kwargs() -> dict[str, Any]:
        try:
            from databricks.sdk.service.sql import Disposition, Format

            return {"disposition": Disposition.INLINE, "format": Format.JSON_ARRAY}
        except ImportError:  # pragma: no cover - fake clients in tests
            return {}

    # --- dialect -----------------------------------------------------------
    def quote(self, ident: str) -> str:
        return "`" + ident.replace("`", "``") + "`"

    def row_checksum(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        return f"SUM(CAST({self.row_hash(columns, types)} AS DECIMAL(38, 0)))"

    def row_hash(self, columns: list[str], types: dict[str, str] | None = None) -> str | None:
        types = types or {}
        return f"xxhash64({', '.join(self.canon(self.quote(c), types.get(c, '')) for c in columns)})"

    def canon(self, expr: str, type_name: str) -> str:
        # Spark can't compare or hash MAP values. Compare a map as its entries sorted by
        # key, as JSON (to_json alone keeps insertion order, so equal maps could differ).
        # Maps nested in structs or arrays are compared as JSON: equal maps written in a
        # different order may show as changed (over-reports, never hides a change).
        t = (type_name or "").lower()
        if t.startswith("map<"):
            return f"to_json(array_sort(map_entries({expr})))"
        if "map<" in t:
            return f"to_json({expr})"
        return expr

    def sample_predicate(self, keys: list[str], pct: float, alias: str | None = None) -> str:
        pre = f"{alias}." if alias else ""
        cols = ", ".join(f"{pre}{self.quote(k)}" for k in keys)
        return f"(abs(xxhash64({cols})) % 10000) < {int(pct * 100)}"

    def days_ago(self, days: int) -> str:
        return f"date_sub(current_date(), {int(days)})"

    # --- metadata ----------------------------------------------------------
    def columns(self, table: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self.query(f"DESCRIBE TABLE {self.qualified(table)}"):
            name = (r.get("col_name") or "").strip()
            if not name or name.startswith("#"):
                break  # partition / detail sections follow
            out[name] = str(r.get("data_type", "")).lower()
        return out

    def clone(self, source: str, target: str) -> None:
        self.execute(
            f"CREATE OR REPLACE TABLE {self.qualified(target)} SHALLOW CLONE {self.qualified(source)}"
        )

    def _name_filter(self, names: list[str], cat_col: str, schema_col: str, name_col: str, loose_single: bool):
        conds = []
        for n in names:
            parts = [p.strip("`").lower() for p in n.split(".")]
            cat = self._lit(parts[0]) if len(parts) == 3 else "lower(current_catalog())"
            if len(parts) >= 2:
                conds.append(f"(lower({cat_col}) = {cat} AND lower({schema_col}) = {self._lit(parts[-2])} "
                             f"AND lower({name_col}) = {self._lit(parts[-1])})")
            elif loose_single:  # an unqualified function name: any schema, to be safe
                conds.append(f"lower({name_col}) = {self._lit(parts[0])}")
            else:
                conds.append(f"(lower({cat_col}) = lower(current_catalog()) AND lower({schema_col}) = "
                             f"lower(current_schema()) AND lower({name_col}) = {self._lit(parts[0])})")
        return " OR ".join(conds) or "FALSE"

    def view_definitions(self, tables: list[str]) -> dict[str, tuple[str | None, str | None, str | None]]:
        # system.information_schema covers every catalog the identity can see.
        where = self._name_filter(tables, "table_catalog", "table_schema", "table_name", loose_single=False)
        rows = self.query("SELECT table_catalog, table_schema, table_name, view_definition "
                          f"FROM system.information_schema.views WHERE {where}")
        out: dict[str, tuple[str | None, str | None, str | None]] = {}
        for t in tables:
            parts = [p.strip("`").lower() for p in t.split(".")]
            for r in rows:
                got = [str(r.get(k, "")).lower() for k in ("table_catalog", "table_schema", "table_name")]
                if got[-len(parts):] == parts:
                    out[t] = (r.get("view_definition"), r.get("table_catalog"), r.get("table_schema"))
        return out

    def function_bodies(self, names: list[str]) -> list[tuple[str, str | None, str, str | None, str | None]]:
        where = self._name_filter(names, "routine_catalog", "routine_schema", "routine_name", loose_single=True)
        rows = self.query("SELECT routine_catalog, routine_schema, routine_name, routine_body, routine_definition "
                          f"FROM system.information_schema.routines WHERE {where}")
        return [(f"{r.get('routine_catalog')}.{r.get('routine_schema')}.{r.get('routine_name')}",
                 r.get("routine_definition"), str(r.get("routine_body") or ""), r.get("routine_catalog"),
                 r.get("routine_schema")) for r in rows]

    def plan_tables(self, sql: str) -> list[str] | None:
        # EXPLAIN EXTENDED plans the statement without running it. The analyzed plan names
        # every relation Spark resolved, including inside SQL functions and views, which
        # no SQL parser can see. Planning errors come back as text, not exceptions.
        rows = self.query(f"EXPLAIN EXTENDED {sql}")
        text = "\n".join(str(v) for r in rows for v in r.values() if v is not None)
        return parse_plan_tables(text)

    def protections(self, tables: list[str]) -> dict[str, dict[str, Any]]:
        # Unity Catalog lists masks and row filters in each catalog's information_schema.
        # One query per catalog. If it can't be read (no UC, no access), say nothing:
        # tables outside UC can't have them.
        out: dict[str, dict[str, Any]] = {}
        by_catalog: dict[str, list[tuple[str, str, str]]] = {}
        for t in tables:
            parts = [p.strip("`") for p in t.split(".")]
            if len(parts) == 3:
                by_catalog.setdefault(parts[0], []).append((t, parts[1].lower(), parts[2].lower()))
        for cat, items in by_catalog.items():
            where = " OR ".join(f"(lower(table_schema) = {self._lit(s)} AND lower(table_name) = {self._lit(n)})"
                                for _, s, n in items)
            sql = (f"SELECT 'mask' AS kind, table_schema, table_name, column_name "
                   f"FROM {self.quote(cat)}.information_schema.column_masks WHERE {where} "
                   f"UNION ALL SELECT 'filter' AS kind, table_schema, table_name, NULL AS column_name "
                   f"FROM {self.quote(cat)}.information_schema.row_filters WHERE {where}")
            try:
                rows = self.query(sql)
            except Exception:
                continue
            for r in rows:
                for t, s, n in items:
                    if str(r.get("table_schema", "")).lower() == s and str(r.get("table_name", "")).lower() == n:
                        e = out.setdefault(t, {"masked": [], "row_filter": False})
                        if r.get("kind") == "mask" and r.get("column_name"):
                            e["masked"].append(str(r["column_name"]))
                        elif r.get("kind") == "filter":
                            e["row_filter"] = True
        return out

    @staticmethod
    def _lit(v: str) -> str:
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def columns_at(self, table: str, version: int | None, strict: bool = False) -> dict[str, str]:
        if version is None:
            return self.columns(table)
        try:
            rows = self.query(f"DESCRIBE QUERY SELECT * FROM {self.at_version(table, version)}")
        except Exception:
            if strict:
                raise
            return self.columns(table)
        out = {(r.get("col_name") or "").strip(): str(r.get("data_type", "")).lower() for r in rows}
        out.pop("", None)
        if not out and strict:
            raise RuntimeError(f"no columns for {table} at version {version}")
        return out or self.columns(table)

    def at_version(self, table: str, version: int | None) -> str:
        if version is None:
            return self.qualified(table)
        return f"{self.qualified(table)} VERSION AS OF {int(version)}"

    def current_version(self, table: str) -> int | None:
        try:
            rows = self.query(f"DESCRIBE HISTORY {self.qualified(table)} LIMIT 1")
        except Exception:
            return None
        if not rows:
            return None
        v = rows[0].get("version")
        return int(v) if v is not None else None

    def history(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(f"DESCRIBE HISTORY {self.qualified(table)} LIMIT {int(limit)}")

    def clone_version(self, source: str, target: str, version: int) -> None:
        self.execute(
            f"CREATE OR REPLACE TABLE {self.qualified(target)} "
            f"SHALLOW CLONE {self.qualified(source)} VERSION AS OF {int(version)}"
        )

    def primary_key(self, table: str) -> list[str]:
        parts = table.split(".")
        if len(parts) != 3:
            return []
        cat, sch, tbl = parts
        lit = lambda x: "'" + x.replace("'", "''") + "'"  # noqa: E731
        try:
            rows = self.query(
                f"SELECT k.column_name FROM {self.quote(cat)}.information_schema.table_constraints c "
                f"JOIN {self.quote(cat)}.information_schema.key_column_usage k "
                "ON k.constraint_catalog = c.constraint_catalog AND k.constraint_schema = c.constraint_schema "
                "AND k.constraint_name = c.constraint_name "
                f"WHERE c.constraint_type = 'PRIMARY KEY' AND c.table_schema = {lit(sch)} "
                f"AND c.table_name = {lit(tbl)} ORDER BY k.ordinal_position"
            )
        except Exception:
            return []
        return [r["column_name"] for r in rows]


def _auth_help(e: Exception, profile: str | None) -> str:
    """One clear message when the Databricks SDK can't find credentials.

    The SDK's own errors can be long or cryptic: for example a CLI started as a subprocess
    of a notebook fails inside the notebook-auth code with a `parent_header` error."""
    detail = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
    what = f"credentials for profile {profile!r} in ~/.databrickscfg" if profile else "Databricks credentials"
    return (
        f"Could not sign in to Databricks: no usable {what} were found ({detail}). "
        "Set DATABRICKS_HOST plus DATABRICKS_TOKEN (or DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET), "
        "or run `databricks auth login`. In a notebook, use the Python API (from bricksentry import Sentry) "
        "instead of calling the CLI in a subprocess, which can't use the notebook's credentials."
    )


def _state(resp: Any) -> str:
    st = getattr(getattr(resp, "status", None), "state", None)
    return getattr(st, "value", st) or "UNKNOWN"


def _type_name(col: Any) -> str:
    t = getattr(col, "type_name", None)
    return str(getattr(t, "value", t) or "STRING").upper()


def _convert(v: Any, t: str) -> Any:
    if v is None:
        return None
    try:
        if t in _INT_TYPES:
            return int(v)
        if t in _FLOAT_TYPES:
            return float(v)
        if t == "BOOLEAN":
            return str(v).lower() == "true"
    except (TypeError, ValueError):
        return v
    return v


class PlanError(RuntimeError):
    pass


_PLAN_NAMES = [
    re.compile(r"\bRelation\s+([^\s\[]+)\["),
    re.compile(r"\bSubqueryAlias\s+([^\s]+)"),
    re.compile(r"\bView\s+\(\s*`?([^,`)]+)`?\s*,"),
]


def parse_plan_tables(text: str) -> list[str]:
    """Table names (two or more parts) from Spark's EXPLAIN EXTENDED output, analyzed plan first."""
    low = text.lower()
    if "error occurred during query planning" in low or "analysisexception" in low or "== analyzed logical plan ==" not in low:
        first = next((ln.strip() for ln in text.splitlines() if "error" in ln.lower()), "no analyzed plan")
        raise PlanError(f"Spark couldn't plan the statement ({first[:200]})")
    start = low.index("== analyzed logical plan ==")
    end = low.find("== optimized logical plan ==", start)
    section = text[start:end if end > 0 else None]
    out: list[str] = []
    for rx in _PLAN_NAMES:
        for m in rx.finditer(section):
            name = m.group(1).replace("`", "").strip().rstrip(",")
            if name.count(".") >= 1 and name.lower() not in [o.lower() for o in out]:
                out.append(name)
    return out
