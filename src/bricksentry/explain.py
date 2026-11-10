"""Plain-English summaries, built from templates (deterministic, no LLM needed).

An agent framework can pass this text to its own LLM if it wants richer prose,
but the verdict never depends on it.
"""

from __future__ import annotations

from bricksentry.models import ImpactNode, TableDiff


def _n(x: int) -> str:
    return f"{x:,}"


def explain_diff(d: TableDiff) -> str:
    if d.error:
        return f"{d.table}: the diff could not run ({d.error})."
    if d.is_identical:
        scope = f" in a {d.sampled_pct:g}% sample" if d.sampled_pct else ""
        return f"{d.table}: no data or schema changes{scope}."

    parts: list[str] = []
    rc = d.candidate_rows - d.base_rows
    if rc:
        parts.append(f"row count {'+' if rc > 0 else ''}{_n(rc)} ({d.row_count_change_pct:+.2f}%)")
    if d.added_rows:
        parts.append(f"{_n(d.added_rows)} rows added")
    if d.removed_rows:
        parts.append(f"{_n(d.removed_rows)} rows removed ({d.removed_pct:.2f}%)")
    if d.changed_rows:
        parts.append(f"{_n(d.changed_rows)} rows changed ({d.changed_pct:.2f}%)")
    s = f"{d.table}: " + ", ".join(parts) if parts else f"{d.table}: schema-only change"

    sch = []
    if d.schema.removed:
        sch.append("dropped " + ", ".join(d.schema.removed))
    if d.schema.added:
        sch.append("added " + ", ".join(d.schema.added))
    if d.schema.type_changed:
        sch.append("retyped " + ", ".join(f"{k} ({a} -> {b})" for k, (a, b) in d.schema.type_changed.items()))
    if sch:
        s += ". Schema: " + "; ".join(sch)

    top = [c for c in sorted(d.columns, key=lambda c: -c.changed_rows) if c.changed_rows][:3]
    if top:
        s += ". Most changed: " + ", ".join(f"{c.column} ({_n(c.changed_rows)})" for c in top)

    moved = [c for c in d.columns if c.sum_change_pct is not None and abs(c.sum_change_pct) >= 0.01]
    moved.sort(key=lambda c: -abs(c.sum_change_pct or 0))
    if moved:
        s += ". Totals: " + ", ".join(f"sum({c.column}) {c.sum_change_pct:+.2f}%" for c in moved[:3])

    nulls = [
        c for c in d.columns
        if c.base_nulls is not None and c.candidate_nulls is not None and c.candidate_nulls > c.base_nulls
    ]
    if nulls:
        s += ". New nulls: " + ", ".join(f"{c.column} +{_n(c.candidate_nulls - c.base_nulls)}" for c in nulls[:3])

    if d.segments:
        by_col: dict[str, list] = {}
        for seg in d.segments:
            by_col.setdefault(seg.column, []).append(seg)
        total = max(d.added_rows + d.removed_rows + d.changed_rows, 1)
        for col, segs in by_col.items():
            lead = segs[0]
            share = lead.affected_rows / total * 100
            if share >= 50:
                s += f". {share:.0f}% of affected rows are {col} = {lead.value}"
            else:
                s += f". Spread across {col}: " + ", ".join(f"{x.value} ({_n(x.affected_rows)})" for x in segs[:3])

    if d.duplicate_keys_candidate > d.duplicate_keys_base:
        s += f". WARNING: {_n(d.duplicate_keys_candidate)} duplicate key(s) on {', '.join(d.keys)}"
    if d.sampled_pct:
        s += f" (based on a {d.sampled_pct:g}% key sample)"
    return s + "."


def explain_impact(table: str, nodes: list[ImpactNode]) -> str:
    if not nodes:
        return f"{table}: nothing downstream found in lineage."
    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    owners = sorted({n.owner for n in nodes if n.owner})
    s = f"{table} feeds " + ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(kinds.items()))
    if owners:
        s += f"; owners: {', '.join(owners[:5])}"
    return s + "."
