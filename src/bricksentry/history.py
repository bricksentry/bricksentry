"""Check a change that already happened, using Delta table history.

``check_change`` answers "what did my last pipeline run do to this table?": it finds the
data change in ``DESCRIBE HISTORY``, makes two shallow clones (the version before and the
version after), diffs them, runs lineage and policy, and returns the assessment with the
``RESTORE`` statement that undoes the change. The table itself is only read.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from bricksentry.assess import Sentry
    from bricksentry.models import Assessment

# Operations that change rows. OPTIMIZE, VACUUM, ZORDER, SET TBLPROPERTIES ... do not.
DATA_OPS = frozenset({
    "WRITE", "MERGE", "UPDATE", "DELETE", "TRUNCATE", "STREAMING UPDATE",
    "CREATE OR REPLACE TABLE AS SELECT", "REPLACE TABLE AS SELECT", "CREATE TABLE AS SELECT",
    "COPY INTO", "RESTORE",
})

_NAME = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){1,2}")


class NoChangeError(ValueError):
    """There is no data change to check (nothing in the history window)."""


@dataclass
class Change:
    """The change being checked: ``base_version`` -> ``version``."""

    table: str
    version: int
    base_version: int
    operation: str
    timestamp: str
    versions: list[int]  # every data-changing version folded into this check
    # Data changes committed after ``version``: restoring to ``base_version`` undoes them too.
    newer: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "version": self.version,
            "base_version": self.base_version,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "versions": self.versions,
            "newer_changes": self.newer,
        }


def is_data_op(operation: Any) -> bool:
    return str(operation or "").strip().upper() in DATA_OPS


def newer_data_changes(history: list[dict[str, Any]], version: int) -> list[dict[str, Any]]:
    """Data changes committed after ``version``, oldest first (OPTIMIZE, VACUUM ... left out)."""
    out = []
    for h in history:
        v = h.get("version")
        if v is None or int(v) <= version or not is_data_op(h.get("operation")):
            continue
        out.append({"version": int(v), "operation": str(h.get("operation")), "timestamp": str(h.get("timestamp") or "")})
    return sorted(out, key=lambda h: h["version"])


def rollback_warning(change: Change, max_listed: int = 3) -> str | None:
    """The warning to show next to the RESTORE when newer data changes exist, else None."""
    if not change.newer:
        return None
    listed = ", ".join(
        f"v{h['version']} ({h['operation']}{', ' + h['timestamp'] if h['timestamp'] else ''})"
        for h in change.newer[-max_listed:][::-1])
    more = len(change.newer) - max_listed
    if more > 0:
        listed += f" and {more} more"
    n = len(change.newer)
    return (f"Newer data changes exist: {listed}. Restoring to v{change.base_version} also undoes "
            f"{'it' if n == 1 else 'them'}. Re-run {'that write' if n == 1 else 'those writes'} after "
            "restoring, or fix forward instead.")


def _ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        if s.isdigit():  # epoch millis, as Databricks job dynamic values give
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        except ValueError:
            raise ValueError(f"not a timestamp: {value!r} (use ISO 8601, e.g. 2026-11-11T09:00:00Z)") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pick_change(
    table: str,
    history: list[dict[str, Any]],
    version: int | None = None,
    since: Any = None,
) -> Change:
    """Choose what to compare from ``DESCRIBE HISTORY`` rows (newest first or any order).

    - ``version``: check exactly that version against the one before it.
    - ``since``: everything after that time, as one change: from the last version committed
      before ``since`` to the newest data change. Use the job's start time to check all a run did.
    - neither: the newest data change against the version before it.
    """
    rows = sorted(
        ({**h, "version": int(h["version"])} for h in history if h.get("version") is not None),
        key=lambda h: h["version"],
        reverse=True,
    )
    if not rows:
        raise NoChangeError(f"{table} has no Delta history.")
    by_version = {h["version"]: h for h in rows}

    if version is not None:
        if version < 1:
            raise NoChangeError("Version 0 is the table's creation; there is nothing before it to compare with.")
        h = by_version.get(version)
        return Change(table, version, version - 1, str((h or {}).get("operation") or "?"),
                      str((h or {}).get("timestamp") or ""), [version])

    data = [h for h in rows if is_data_op(h.get("operation")) and h["version"] > 0]
    if since is not None and since != "":
        start = _ts(since)
        after = [h for h in data if (_ts(h.get("timestamp")) or start) >= start]
        if not after:
            raise NoChangeError(f"No data change to {table} since {since}.")
        newest, oldest = after[0], after[-1]
        ops = sorted({str(h.get("operation")) for h in after})
        return Change(table, newest["version"], oldest["version"] - 1,
                      ops[0] if len(ops) == 1 else " + ".join(ops),
                      str(newest.get("timestamp") or ""), [h["version"] for h in reversed(after)])

    if not data:
        raise NoChangeError(
            f"No data change in the last {len(rows)} versions of {table} "
            "(only operations like OPTIMIZE or VACUUM). Pass a version to check a specific one.")
    h = data[0]
    return Change(table, h["version"], h["version"] - 1, str(h.get("operation")),
                  str(h.get("timestamp") or ""), [h["version"]])


def _scratch_name(schema: str, table: str, version: int, run: str) -> str:
    short = re.sub(r"[^A-Za-z0-9_]", "_", table.split(".")[-1])[:40]
    return f"{schema}.bs_check_{short}_v{version}_{run}"


def _missing_catalog(e: Exception) -> bool:
    m = str(e).upper()
    return any(k in m for k in ("NO_SUCH_CATALOG", "CATALOG_NOT_FOUND", "CATALOG DOES NOT EXIST"))


def _ensure_scratch(db: Any, schema: str) -> None:
    try:
        db.ensure_schema(schema)
    except Exception as e:
        if _missing_catalog(e):
            where = f"catalog {schema.split('.')[0]!r}" if "." in schema else "the default catalog"
            raise RuntimeError(
                f"The scratch schema {schema} is in {where}, which doesn't exist in this "
                "workspace or you can't use it. Set the scratch schema (sandbox_schema, or --scratch-schema in a "
                "job) to catalog.schema in a catalog you can create tables in.") from None
        raise


def check_change(
    sentry: Sentry,
    table: str,
    keys: list[str] | None = None,
    version: int | None = None,
    since: Any = None,
    sample_pct: float | None = None,
    history_limit: int = 50,
) -> tuple[Assessment, Change]:
    """Assess a change that already landed in ``table``. See the module docstring."""
    db = sentry.backend
    if not db.supports_time_travel:
        raise NotImplementedError(
            f"check-last-change needs Delta table history; the {db.name} backend has none. "
            "Use the databricks backend.")
    if not _NAME.fullmatch(table):
        raise ValueError(f"table must be schema.table or catalog.schema.table; got {table!r}")

    history = db.history(table, history_limit)
    change = pick_change(table, history, version=version, since=since)
    keys = keys or sentry.config.spec(table).keys or db.primary_key(table)
    if not keys:
        raise ValueError(
            f"No key columns for {table}: set tables.{table}.keys in bricksentry.yml, pass --key, "
            "or add a PRIMARY KEY constraint to the table.")

    scratch = sentry.config.sandbox_schema
    # Unique per check, so two checks of the same table at the same time (two jobs, or a job
    # and a notebook) never drop each other's clones.
    run = uuid.uuid4().hex[:8]
    base = _scratch_name(scratch, table, change.base_version, run)
    cand = _scratch_name(scratch, table, change.version, run)
    _ensure_scratch(db, scratch)
    try:
        for target, v in ((base, change.base_version), (cand, change.version)):
            try:
                db.clone_version(table, target, v)
            except Exception as e:
                raise RuntimeError(
                    f"Could not read version {v} of {table}: its files may have been removed by VACUUM, "
                    f"or you may lack CREATE TABLE on {scratch}. ({str(e).strip()[:300]})") from None
        d = sentry.diff(base, cand, keys=list(keys), table=table, sample_pct=sample_pct)
        a = sentry.assess([table], diffs={table: d})
    finally:
        for t in (base, cand):
            try:
                db.drop(t)
            except Exception:  # pragma: no cover - best effort cleanup
                pass

    # The change already happened, so undoing it means going back to the version before it.
    # That also undoes anything written after it, so read the history again (writes may have
    # landed while the check ran) and warn about newer data changes next to the RESTORE.
    try:
        latest = db.history(table, history_limit)
    except Exception:  # pragma: no cover - keep the first read if a second one fails
        latest = history
    change.newer = newer_data_changes(latest, change.version)
    warning = rollback_warning(change)
    undo = f"RESTORE TABLE {db.qualified(table)} TO VERSION AS OF {change.base_version}"
    for t in a.tables:
        t.rollback = undo
        t.rollback_warning = warning
        t.summary = f"v{change.base_version} -> v{change.version} ({change.operation}): {t.summary}"
    sentry._save(a)
    return a, change
