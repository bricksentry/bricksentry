"""Policy-as-code. Deterministic: the verdict always comes from these rules,
never from an LLM, so agents and CI get the same answer for the same change.

``policy.yml``::

    default: pass
    notify_owners_on: needs_human
    rules:
      - name: duplicate-keys
        when: {duplicate_keys: true}
        then: block
      - name: dropped-column
        when: {column_removed: true}
        then: needs_human
      - name: certified-downstream
        when: {downstream_tag: certified}
        then: needs_human
      - name: big-row-loss
        when: {removed_pct_gt: 1}
        then: needs_human
      - name: revenue-moved
        when: {column: amount, sum_change_pct_gt: 2}
        then: needs_human
      - name: wide-blast-radius
        when: {downstream_count_gt: 25}
        then: warn

All conditions inside one ``when`` must hold (AND). Use several rules for OR.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from bricksentry.models import ImpactNode, RuleHit, TableDiff, Verdict

DEFAULT_POLICY: dict[str, Any] = {
    "default": "pass",
    "notify_owners_on": "needs_human",
    "rules": [
        {"name": "diff-failed", "when": {"diff_error": True}, "then": "needs_human"},
        {"name": "duplicate-keys", "when": {"duplicate_keys": True}, "then": "block"},
        {"name": "dropped-column", "when": {"column_removed": True}, "then": "needs_human"},
        {"name": "type-change", "when": {"column_type_changed": True}, "then": "warn"},
        {"name": "certified-downstream", "when": {"downstream_tag": "certified"}, "then": "needs_human"},
        {"name": "pii-table", "when": {"tag": "pii"}, "then": "needs_human"},
        {"name": "row-loss", "when": {"removed_pct_gt": 1}, "then": "needs_human"},
        {"name": "many-rows-changed", "when": {"changed_pct_gt": 10}, "then": "warn"},
        {"name": "numeric-total-moved", "when": {"sum_change_pct_gt": 5}, "then": "warn"},
        {"name": "dashboard-downstream", "when": {"downstream_kind": "dashboard", "any_change": True}, "then": "warn"},
        {"name": "wide-blast-radius", "when": {"downstream_count_gt": 25}, "then": "warn"},
    ],
}

KNOWN_CONDITIONS = {
    "table", "tag", "downstream_tag", "downstream_kind", "downstream_count_gt",
    "removed_pct_gt", "added_pct_gt", "changed_pct_gt", "row_count_change_pct_gt",
    "row_count_drop_pct_gt", "column_removed", "column_type_changed", "column_added",
    "duplicate_keys", "column", "sum_change_pct_gt", "null_increase_gt",
    "any_change", "diff_error",
}


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, spec: dict[str, Any] | None = None):
        spec = spec if spec is not None else DEFAULT_POLICY
        self.default = Verdict(spec.get("default", "pass"))
        self.notify_on = Verdict(spec.get("notify_owners_on", "needs_human"))
        self.rules = spec.get("rules") or []
        for r in self.rules:
            unknown = set((r.get("when") or {}).keys()) - KNOWN_CONDITIONS
            if unknown:
                raise PolicyError(f"Rule {r.get('name')!r} uses unknown condition(s): {sorted(unknown)}")
            Verdict(r.get("then", "warn"))

    def evaluate(
        self,
        table: str,
        diff: TableDiff | None,
        downstream: list[ImpactNode],
        tags: dict[str, str],
        diff_error: str | None = None,
    ) -> tuple[Verdict, list[RuleHit]]:
        hits: list[RuleHit] = []
        for r in self.rules:
            ok, reason = _match(r.get("when") or {}, table, diff, downstream, tags, diff_error)
            if ok:
                hits.append(RuleHit(rule=r.get("name", "unnamed"), verdict=Verdict(r.get("then", "warn")), table=table, reason=reason))
        verdict = Verdict.worst([self.default] + [h.verdict for h in hits])
        return verdict, hits


def _has_tag(tags: dict[str, str], want: Any) -> bool:
    if isinstance(want, dict):
        return all(str(tags.get(k, "")).lower() == str(v).lower() for k, v in want.items())
    want = str(want).lower()
    for k, v in tags.items():
        if k.lower() == want and str(v).lower() not in ("false", "no", "0"):
            return True
        if str(v).lower() == want:
            return True
    return False


def _match(
    when: dict[str, Any],
    table: str,
    diff: TableDiff | None,
    downstream: list[ImpactNode],
    tags: dict[str, str],
    diff_error: str | None,
) -> tuple[bool, str]:
    reasons: list[str] = []
    for cond, val in when.items():
        if cond == "column":
            continue  # modifier for sum_change_pct_gt / null_increase_gt
        if cond == "diff_error":
            if bool(diff_error) != bool(val):
                return False, ""
            reasons.append(f"diff could not run: {diff_error}")
            continue
        if cond == "table":
            if not fnmatch.fnmatch(table.lower(), str(val).lower()):
                return False, ""
            continue
        if cond == "tag":
            if not _has_tag(tags, val):
                return False, ""
            reasons.append(f"table is tagged {val}")
            continue
        if cond == "downstream_tag":
            m = [n.name for n in downstream if _has_tag(n.tags, val)]
            if not m:
                return False, ""
            reasons.append(f"{len(m)} downstream asset(s) tagged {val}: {', '.join(m[:3])}")
            continue
        if cond == "downstream_kind":
            m = [n.name for n in downstream if n.kind == str(val).lower()]
            if not m:
                return False, ""
            reasons.append(f"{len(m)} downstream {val}(s): {', '.join(m[:3])}")
            continue
        if cond == "downstream_count_gt":
            if len(downstream) <= float(val):
                return False, ""
            reasons.append(f"{len(downstream)} downstream assets")
            continue

        # Everything below needs a diff.
        if diff is None:
            return False, ""
        if cond == "any_change":
            if diff.is_identical == bool(val):
                return False, ""
            continue
        if cond == "duplicate_keys":
            dup = diff.duplicate_keys_candidate > diff.duplicate_keys_base
            if dup != bool(val):
                return False, ""
            reasons.append(f"{diff.duplicate_keys_candidate} duplicate key(s) in candidate (base had {diff.duplicate_keys_base})")
            continue
        if cond == "column_removed":
            if bool(diff.schema.removed) != bool(val):
                return False, ""
            reasons.append(f"columns removed: {', '.join(diff.schema.removed)}")
            continue
        if cond == "column_added":
            if bool(diff.schema.added) != bool(val):
                return False, ""
            reasons.append(f"columns added: {', '.join(diff.schema.added)}")
            continue
        if cond == "column_type_changed":
            if bool(diff.schema.type_changed) != bool(val):
                return False, ""
            reasons.append(
                "type changes: " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in diff.schema.type_changed.items())
            )
            continue
        if cond in ("removed_pct_gt", "added_pct_gt", "changed_pct_gt"):
            attr = cond.replace("_gt", "")
            pct = getattr(diff, attr)
            if pct <= float(val):
                return False, ""
            reasons.append(f"{attr.replace('_pct', '')} {pct:.2f}% of rows (> {val}%)")
            continue
        if cond == "row_count_change_pct_gt":
            if abs(diff.row_count_change_pct) <= float(val):
                return False, ""
            reasons.append(f"row count changed {diff.row_count_change_pct:+.2f}%")
            continue
        if cond == "row_count_drop_pct_gt":
            if -diff.row_count_change_pct <= float(val):
                return False, ""
            reasons.append(f"row count dropped {-diff.row_count_change_pct:.2f}%")
            continue
        if cond == "sum_change_pct_gt":
            col = when.get("column")
            cols = [c for c in diff.columns if (col is None or c.column == col) and c.sum_change_pct is not None]
            m = [c for c in cols if abs(c.sum_change_pct) > float(val)]
            if not m:
                return False, ""
            reasons.append(", ".join(f"sum({c.column}) {c.sum_change_pct:+.2f}%" for c in m[:3]))
            continue
        if cond == "null_increase_gt":
            col = when.get("column")
            m = [
                c for c in diff.columns
                if (col is None or c.column == col)
                and c.base_nulls is not None and c.candidate_nulls is not None
                and (c.candidate_nulls - c.base_nulls) > float(val)
            ]
            if not m:
                return False, ""
            reasons.append(", ".join(f"{c.column} nulls +{c.candidate_nulls - c.base_nulls}" for c in m[:3]))
            continue
    return True, "; ".join(reasons) or "matched"
