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


def scrub(x: Any, key: str | None = None) -> Any:
    """Remove values from any result dict (see the module docstring)."""
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
                out[k] = scrub(v, k)
        return out
    if isinstance(x, list):
        return [scrub(v, key) for v in x]
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
