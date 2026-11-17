"""What an AI agent is allowed to see.

bricksentry usually runs with more read access than the agent calling it (the MCP
server or a job identity can read production; the agent should not). So everything
returned to an agent goes through :func:`for_agent`:

* no row values: ``samples`` (before -> after values) are removed;
* no absolute totals: ``base_sum`` / ``candidate_sum`` are removed and the percentage
  change is rounded to one decimal (a total over a few rows *is* the values);
* no segment values: segments keep their column and row count, not the value;
* engine errors lose quoted literals (``The value '123-45-6789' cannot be cast``);
* summaries are rebuilt without values.

Counts, column names, schema, verdicts and rule reasons stay: the agent needs them to
act. Counts can still be used as an oracle (preview ``DELETE ... WHERE ssn LIKE '1%'``
and read the row count), which no masking can stop, so ``agent_tables`` in
bricksentry.yml limits which tables an agent may assess or preview at all.
Humans (CLI, reports, pull-request comments) keep the full detail, except values in
pii-tagged tables and columns, which reports never show.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from bricksentry.explain import explain_diff, redact_values

if TYPE_CHECKING:
    from bricksentry.assess import Sentry
    from bricksentry.models import Assessment

log = logging.getLogger("bricksentry.audit")

_DROP = {"samples", "base_sum", "candidate_sum"}


def for_agent(a: Assessment, sentry: Sentry, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The agent-safe dict of an assessment."""
    out = a.to_dict()
    for t_obj, t in zip(a.tables, out.get("tables") or [], strict=False):
        if t_obj.diff is not None:
            measures = sentry.config.spec(t_obj.table).measures
            human = explain_diff(t_obj.diff, measures)
            rest = t_obj.summary[len(human):] if t_obj.summary.startswith(human) else ""
            t["summary"] = explain_diff(t_obj.diff, measures, for_agent=True) + rest
    if extra:
        out.update(extra)
    return scrub(out)


_SIMPLE = re.compile(r"^[\w.\-:/@+]{0,64}(?: [\w.\-:/@+]{1,64})?$")


def _plain(text: Any) -> Any:
    """Tag names and values, owners: kept when short and simple ("pii", "true",
    "finance-team", "a@b.com"); anything longer or free text is replaced, because
    metadata anyone can write could carry instructions aimed at the agent."""
    if text is None or isinstance(text, (bool, int, float)):
        return text
    t = str(text)
    if len(t) <= 64 and _SIMPLE.match(t):
        return t
    return f"[text hidden: {len(t)} chars]"


def _tags(d: Any) -> Any:
    if not isinstance(d, dict):
        return d
    return {_plain(k): _plain(v) for k, v in d.items()}


def scrub_metadata(x: Any, key: str | None = None) -> Any:
    """Free text in tags and owners is replaced (see :func:`_plain`), also where it was
    copied into other text such as a summary ("owners: ..."); everything else stays."""
    bad: set[str] = set()
    _collect(x, bad)
    out = _scrub_meta(x, key)
    if bad:
        out = _replace_all(out, sorted(bad, key=len, reverse=True))
    return out


def _collect(x: Any, bad: set[str], key: str | None = None) -> None:
    def note(t: Any) -> None:
        if isinstance(t, str) and _plain(t) != t:
            bad.add(t)

    if isinstance(x, dict):
        for k, v in x.items():
            if k == "tags" and isinstance(v, dict):
                for tk, tv in v.items():
                    note(tk)
                    note(tv)
            elif k == "column_tags" and isinstance(v, dict):
                for tags in v.values():
                    if isinstance(tags, dict):
                        for tk, tv in tags.items():
                            note(tk)
                            note(tv)
            elif k in ("owner", "owners", "notify"):
                for o in v if isinstance(v, list) else [v]:
                    note(o)
            else:
                _collect(v, bad, k)
    elif isinstance(x, list):
        for v in x:
            _collect(v, bad, key)


def _replace_all(x: Any, bad: list[str]) -> Any:
    if isinstance(x, dict):
        return {k: _replace_all(v, bad) for k, v in x.items()}
    if isinstance(x, list):
        return [_replace_all(v, bad) for v in x]
    if isinstance(x, str):
        for b in bad:
            if b in x:
                x = x.replace(b, _plain(b))
    return x


def _scrub_meta(x: Any, key: str | None = None) -> Any:
    if isinstance(x, dict):
        out: dict[str, Any] = {}
        for k, v in x.items():
            if k == "tags":
                out[k] = _tags(v)
            elif k == "column_tags" and isinstance(v, dict):
                out[k] = {c: _tags(t) for c, t in v.items()}
            elif k in ("owner", "owners", "notify"):
                out[k] = [_plain(o) for o in v] if isinstance(v, list) else _plain(v)
            else:
                out[k] = _scrub_meta(v, k)
        return out
    if isinstance(x, list):
        return [_scrub_meta(v, key) for v in x]
    return x


def scrub(x: Any, key: str | None = None) -> Any:
    """Remove values from any result dict (see the module docstring), and free text from metadata."""
    return scrub_metadata(_scrub_values(x, key))


def _scrub_values(x: Any, key: str | None = None) -> Any:
    if isinstance(x, dict):
        out: dict[str, Any] = {}
        for k, v in x.items():
            if k in _DROP:
                continue
            if k == "sum_change_pct" and isinstance(v, (int, float)):
                out[k] = round(v, 1)
            elif k == "segments" and isinstance(v, list):
                out[k] = [{"column": s.get("column"), "affected_rows": s.get("affected_rows")}
                          for s in v if isinstance(s, dict)]
            else:
                out[k] = _scrub_values(v, k)
        return out
    if isinstance(x, list):
        return [_scrub_values(v, key) for v in x]
    if isinstance(x, str) and key in ("error", "notes", "reason", "summary", "rollback_warning"):
        return redact_values(x)
    return x


def allowed(sentry: Sentry, table: str) -> bool:
    """Whether ``agent_tables`` (glob patterns, any case) lets an agent assess or preview ``table``.
    Unset means every table, as before 0.1.27."""
    patterns = sentry.config.agent_tables
    if patterns is None:
        return True
    name = table.lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns)


def tables_read(sql: str, dialect: str) -> list[str]:
    """Every table a statement touches, as written (``catalog.schema.table``), via a real
    SQL parser. Raises PermissionError for anything that can't be checked by name:
    unparseable SQL, table functions (``table_changes(...)``, ``read_files(...)``,
    ``IDENTIFIER(...)``) and path-based tables (``delta.`/path```)."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - sqlglot is a dependency
        raise PermissionError("Can't check which tables this statement reads (sqlglot is not installed).") from None
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception:
        raise PermissionError(
            "Couldn't parse the statement to check which tables it reads, so it's refused for agents. "
            "Simplify it (plain table names, no dynamic SQL)."
        ) from None
    if len(trees) != 1:
        raise PermissionError("Exactly one statement is allowed.")
    tree = trees[0]
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[str] = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise PermissionError(
                "Table functions and dynamic table names (table_changes, read_files, IDENTIFIER, ...) can't be "
                "checked against agent_tables, so they're refused for agents. Name the tables directly."
            )
        parts = [x for x in (t.catalog, t.db, t.name) if x]
        name = ".".join(parts)
        if not t.catalog and not t.db and t.name.lower() in ctes:
            continue
        if "/" in name or ":" in name:
            raise PermissionError(f"Path-based tables ({name}) are refused for agents. Use a Unity Catalog table name.")
        out.append(name)
    return out


def check_statement(sentry: Sentry, sql: str) -> None:
    """With ``agent_tables`` set, every table the statement reads or writes must be listed.
    A table named with fewer parts than the pattern (``sales.orders`` for ``main.sales.*``)
    doesn't match: write the full name."""
    if sentry.config.agent_tables is None:
        return
    dialect = "duckdb" if getattr(sentry.backend, "name", "") == "duckdb" else "databricks"
    check_allowed(sentry, tables_read(sql, dialect))


def check_allowed(sentry: Sentry, tables: list[str]) -> None:
    refused = [t for t in tables if not allowed(sentry, t)]
    if refused:
        raise PermissionError(
            f"Not allowed for agents: {', '.join(refused)}. This bricksentry only lets agents assess "
            "or preview the tables listed under agent_tables in bricksentry.yml."
        )


def audit(sentry: Sentry, tool: str, tables: list[str], result: dict[str, Any] | None = None,
          refused: str | None = None) -> None:
    """One line per agent assess or preview: who, which tables, what came back.
    Logged to ``bricksentry.audit`` and appended to ``audit.jsonl`` in the assessment store."""
    from bricksentry.assess import _actor

    rows = {}
    for t in (result or {}).get("tables") or []:
        d = t.get("diff") or {}
        rows[t.get("table")] = {k: d.get(k) for k in ("added_rows", "removed_rows", "changed_rows")}
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "actor": _actor(),
        "tables": tables,
        "verdict": (result or {}).get("verdict"),
        "assessment_id": (result or {}).get("id"),
        "rows": rows or None,
        "refused": refused,
    }
    log.info(json.dumps(entry))
    folder = sentry.config.store_dir()
    if folder is None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "audit.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
