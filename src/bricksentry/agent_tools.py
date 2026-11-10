"""Agent tools as plain Python functions returning JSON-safe dicts.

The MCP server registers these, but they work in any agent framework
(LangGraph, OpenAI Agents SDK, Databricks Agent Framework...): import and wrap.

Errors come back as ``{"error": "..."}`` instead of exceptions, so an agent
can read them and recover.
"""

from __future__ import annotations

import functools
from typing import Any

from bricksentry.assess import Sentry
from bricksentry.explain import explain_diff as _explain_diff

_SENTRY: Sentry | None = None


def get_sentry() -> Sentry:
    global _SENTRY
    if _SENTRY is None:
        _SENTRY = Sentry()
    return _SENTRY


def set_sentry(sentry: Sentry | None) -> None:
    global _SENTRY
    _SENTRY = sentry


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    return wrapper


@_safe
def whats_downstream(table: str, depth: int = 3) -> dict[str, Any]:
    """List everything downstream of a table (tables, jobs, notebooks, pipelines,
    dashboards, queries) with owners and tags, from lineage. Read-only."""
    s = get_sentry()
    nodes = s.downstream(table, depth)
    owner, tags = s.lineage.owner_and_tags(table)
    return {
        "table": table,
        "owner": owner,
        "tags": tags,
        "count": len(nodes),
        "downstream": [n.to_dict() for n in nodes],
    }


@_safe
def assess_change(
    tables: list[str],
    candidates: dict[str, str] | None = None,
    pr: str | None = None,
) -> dict[str, Any]:
    """Assess a change: diff each table's production version against its candidate
    (built by your change), walk downstream lineage and apply policy.
    Returns a verdict (pass / warn / needs_human / block), a risk tier, reasons,
    owners to notify and an assessment id for explain_diff / safe_to_ship. Read-only."""
    a = get_sentry().assess(list(tables), candidates=candidates, pr=pr)
    return a.to_dict()


@_safe
def preview_write(sql: str, target: str, keys: list[str] | None = None) -> dict[str, Any]:
    """Run ONE write statement (INSERT / MERGE / UPDATE / DELETE / CREATE OR REPLACE TABLE)
    against a disposable sandbox clone of `target`, never production, and report
    what it would change plus the policy verdict. Use {{target}} in the SQL or the
    literal table name. The clone is dropped afterwards."""
    return get_sentry().preview_write(sql, target, keys=keys)


@_safe
def explain_diff(assessment_id: str | None = None, table: str | None = None) -> dict[str, Any]:
    """Plain-English explanation of what changed, by assessment id or table name. Read-only."""
    s = get_sentry()
    if assessment_id:
        a = s.get_assessment(assessment_id)
        if a is None:
            return {"error": f"unknown assessment id {assessment_id}"}
        return {"assessment_id": a.id, "explanations": {t.table: t.summary for t in a.tables}}
    if table:
        d = s.get_diff(table)
        if d is None:
            return {"error": f"no diff for {table} yet; call assess_change or preview_write first"}
        return {"table": table, "explanation": _explain_diff(d)}
    return {"error": "pass assessment_id or table"}


@_safe
def safe_to_ship(assessment_id: str) -> dict[str, Any]:
    """Final gate for an assessment: returns safe_to_ship (bool), the verdict,
    the rules that fired and the next step the agent should take. Read-only."""
    a = get_sentry().get_assessment(assessment_id)
    if a is None:
        return {"error": f"unknown assessment id {assessment_id}"}
    v = a.verdict.value
    next_step = {
        "pass": "Proceed: open or merge the pull request.",
        "warn": "Proceed, but include the warnings in the pull request description.",
        "needs_human": "Do not merge. Open the pull request with the report and request review from: "
        + (", ".join(a.notify) or "a data owner") + ".",
        "block": "Stop. Fix the blocking issues listed in rule_hits, rebuild the candidate and assess again.",
    }[v]
    return {
        "assessment_id": a.id,
        "safe_to_ship": v in ("pass", "warn"),
        "verdict": v,
        "risk_tier": a.verdict.risk_tier,
        "rule_hits": [h.to_dict() for h in a.hits],
        "notify": a.notify,
        "next_step": next_step,
    }


@_safe
def rollback_plan(table: str) -> dict[str, Any]:
    """The exact SQL to restore `table` to its current version if a change goes wrong.
    Capture it BEFORE applying a change. Read-only."""
    s = get_sentry()
    return {
        "table": table,
        "current_version": s.backend.current_version(table),
        "rollback_sql": s.rollback_plan(table),
    }


TOOLS = [whats_downstream, assess_change, preview_write, explain_diff, safe_to_ship, rollback_plan]
READ_ONLY = {"whats_downstream", "assess_change", "explain_diff", "safe_to_ship", "rollback_plan"}
