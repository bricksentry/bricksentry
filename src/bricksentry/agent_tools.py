"""Agent tools as plain Python functions returning JSON-safe dicts.

The MCP server registers these, but they work in any agent framework
(LangGraph, OpenAI Agents SDK, Databricks Agent Framework...): import and wrap.

Errors come back as ``{"error": "..."}`` instead of exceptions, so an agent
can read them and recover.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any

from bricksentry.assess import Sentry
from bricksentry.explain import explain_diff as _explain_diff

try:  # pydantic ships with the `mcp` extra; parameter descriptions are optional without it
    from pydantic import Field as _Field
except ImportError:  # pragma: no cover
    _Field = None


def _desc(text: str):
    """Parameter description that MCP servers expose in the tool schema."""
    return _Field(description=text) if _Field is not None else text


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
def whats_downstream(
    table: Annotated[str, _desc("Fully qualified table name, e.g. main.sales.orders.")],
    depth: Annotated[int, _desc("How many lineage hops to follow downstream (default 3).")] = 3,
) -> dict[str, Any]:
    """List what depends on a table, so you know the blast radius BEFORE you edit it.

    Use this first, when planning a change. It reads Unity Catalog lineage and
    returns every downstream table, job, notebook, pipeline, dashboard and query,
    up to `depth` hops, with owners and tags. Dropped tables are filtered out.
    Read-only; changes nothing. To check a change you have already made, use
    assess_change instead.

    Returns {table, owner, tags, count, downstream: [{kind, name, depth, via,
    owner, tags}]}, or {error} if the table or lineage cannot be read."""
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
    tables: Annotated[list[str], _desc("Production tables your change affects, fully qualified.")],
    candidates: Annotated[
        dict[str, str] | None,
        _desc(
            "Optional map of production table -> candidate table your change built "
            "(e.g. {'main.sales.orders': 'dev.sales.orders'}). Omit to use the "
            "candidate pattern from bricksentry.yml."
        ),
    ] = None,
    pr: Annotated[str | None, _desc("Optional pull request number or id, used to name candidates and the report.")] = None,
) -> dict[str, Any]:
    """Assess a change that has already been built into candidate tables.

    Use this after your pipeline change has written candidate (dev/PR) tables.
    For a single SQL write that has not run yet, use preview_write instead.
    For each table it diffs production against the candidate (row, column and
    schema changes, null-safe keys), walks downstream lineage and applies the
    rules in policy.yml. The verdict is deterministic: rules decide, not an LLM.
    Read-only on production and candidates.

    Returns {id, verdict: pass|warn|needs_human|block, risk_tier, safe_to_ship,
    notify, rule_hits, tables: [{table, verdict, summary, diff, downstream,
    rollback}]}. Pass `id` to safe_to_ship (and optionally explain_diff)."""
    a = get_sentry().assess(list(tables), candidates=candidates, pr=pr)
    return a.to_dict()


@_safe
def preview_write(
    sql: Annotated[
        str,
        _desc(
            "Exactly one INSERT, MERGE, UPDATE, DELETE or CREATE OR REPLACE TABLE "
            "statement. Write {{target}} where the table name goes, or use the literal name."
        ),
    ],
    target: Annotated[str, _desc("Fully qualified production table the statement would write to.")],
    keys: Annotated[
        list[str] | None, _desc("Optional key columns for the row diff. Omit to use keys from bricksentry.yml.")
    ] = None,
) -> dict[str, Any]:
    """Dry-run ONE write statement on a disposable shallow clone of `target`,
    never on production, and report what it would change plus the policy verdict.

    Use this before running any direct write. It clones the table, rewrites the
    statement to hit the clone, runs it, diffs clone vs production, applies policy
    and drops the clone, even on failure. DROP, TRUNCATE, ALTER, GRANT and
    multiple statements are refused.

    Returns the same shape as assess_change (with an `id` for safe_to_ship), or
    {error} if the statement is refused or fails."""
    return get_sentry().preview_write(sql, target, keys=keys)


@_safe
def explain_diff(
    assessment_id: Annotated[
        str | None, _desc("Id returned by assess_change or preview_write. Give this or `table`.")
    ] = None,
    table: Annotated[
        str | None, _desc("Table name, to explain its most recent diff in this session. Give this or `assessment_id`.")
    ] = None,
) -> dict[str, Any]:
    """Explain in plain English what a diff changed, for a PR description or a
    human reviewer.

    Use after assess_change or preview_write when you need readable text rather
    than raw numbers, e.g. "removed 1 row (0.00%); revenue sum -3.1%". Read-only.
    Works across processes because assessments are saved to disk.

    Returns {assessment_id, explanations: {table: text}} when given an id, or
    {table, explanation} when given a table, or {error} if nothing matches."""
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
def safe_to_ship(
    assessment_id: Annotated[str, _desc("Id returned by assess_change or preview_write.")],
) -> dict[str, Any]:
    """Final gate: decide whether to merge or apply a change. Call this last,
    and follow `next_step` exactly.

    safe_to_ship is true only for pass and warn. For needs_human, do not merge:
    open the PR with the report and request review from the listed owners. For
    block, stop and fix the issues in rule_hits. Never bypass needs_human or
    block. Read-only.

    Returns {assessment_id, safe_to_ship: bool, verdict, risk_tier, rule_hits:
    [{rule, verdict, table, reason}], notify, next_step}, or {error} if the id
    is unknown."""
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
def rollback_plan(
    table: Annotated[str, _desc("Fully qualified Delta table name.")],
) -> dict[str, Any]:
    """Get the exact SQL to restore `table` to its current Delta version.

    Call this BEFORE applying a change and keep the SQL (e.g. in the PR or job
    log), so undo is one statement if the change goes wrong. Read-only: it reads
    DESCRIBE HISTORY and runs nothing.

    Returns {table, current_version, rollback_sql}, e.g. "RESTORE TABLE
    main.sales.orders TO VERSION AS OF 42", or {error}."""
    s = get_sentry()
    return {
        "table": table,
        "current_version": s.backend.current_version(table),
        "rollback_sql": s.rollback_plan(table),
    }


TOOLS = [whats_downstream, assess_change, preview_write, explain_diff, safe_to_ship, rollback_plan]
READ_ONLY = {"whats_downstream", "assess_change", "explain_diff", "safe_to_ship", "rollback_plan"}
