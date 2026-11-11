"""Databricks backend: runs SQL on a SQL warehouse via the Statement Execution API.

Auth uses the standard Databricks SDK chain (env vars, ~/.databrickscfg profile,
OAuth, Azure/GCP identity...). Nothing in bricksentry handles credentials itself.
"""

from __future__ import annotations

import os
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

    def row_checksum(self, columns: list[str]) -> str | None:
        cols = ", ".join(self.quote(c) for c in columns)
        return f"SUM(CAST(xxhash64({cols}) AS DECIMAL(38, 0)))"

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

    def current_version(self, table: str) -> int | None:
        try:
            rows = self.query(f"DESCRIBE HISTORY {self.qualified(table)} LIMIT 1")
        except Exception:
            return None
        if not rows:
            return None
        v = rows[0].get("version")
        return int(v) if v is not None else None


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
