"""Ready-made policies for common patterns.

Use one in ``bricksentry.yml``::

    policy: builtin:pii

or write it to a file you can edit: ``bricksentry init --policy pii``.
List them with ``bricksentry policies``.

Every template keeps the core safety rules (failed diffs, duplicate keys,
NULL keys, dropped columns) and adds rules for its pattern.
"""

from __future__ import annotations

import copy
from typing import Any

from bricksentry.policy import DEFAULT_POLICY

_CORE = [
    {"name": "diff-failed", "when": {"diff_error": True}, "then": "needs_human"},
    {"name": "duplicate-keys", "when": {"duplicate_keys": True}, "then": "block"},
    {"name": "null-keys", "when": {"null_keys": True}, "then": "warn"},
    {"name": "dropped-column", "when": {"column_removed": True}, "then": "needs_human"},
    {"name": "type-change", "when": {"column_type_changed": True}, "then": "warn"},
]

TEMPLATES: dict[str, dict[str, Any]] = {
    "default": {
        "description": "Balanced defaults: block duplicate keys, ask a human for row loss, dropped columns, PII and certified downstream.",
        "policy": DEFAULT_POLICY,
    },
    "strict": {
        "description": "Production-critical pipelines: small changes need a human, failed checks block.",
        "policy": {
            "default": "pass",
            "notify_owners_on": "warn",
            "rules": [
                {"name": "diff-failed", "when": {"diff_error": True}, "then": "block"},
                {"name": "lineage-unavailable", "when": {"lineage_error": True}, "then": "needs_human"},
                {"name": "duplicate-keys", "when": {"duplicate_keys": True}, "then": "block"},
                {"name": "null-keys", "when": {"null_keys": True}, "then": "needs_human"},
                {"name": "dropped-column", "when": {"column_removed": True}, "then": "block"},
                {"name": "type-change", "when": {"column_type_changed": True}, "then": "needs_human"},
                {"name": "row-loss", "when": {"removed_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "row-count-drop", "when": {"row_count_drop_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "many-rows-changed", "when": {"changed_pct_gt": 2}, "then": "needs_human"},
                {"name": "numeric-total-moved", "when": {"sum_change_pct_gt": 1}, "then": "needs_human"},
                {"name": "certified-downstream", "when": {"downstream_tag": "certified", "any_change": True}, "then": "needs_human"},
                {"name": "wide-blast-radius", "when": {"downstream_count_gt": 10}, "then": "warn"},
            ],
        },
    },
    "agent-safe": {
        "description": "Changes written by AI agents: anything beyond a small, clean change goes to a human before it ships.",
        "policy": {
            "default": "pass",
            "notify_owners_on": "needs_human",
            "rules": [
                {"name": "diff-failed", "when": {"diff_error": True}, "then": "block"},
                {"name": "lineage-unavailable", "when": {"lineage_error": True}, "then": "needs_human"},
                {"name": "duplicate-keys", "when": {"duplicate_keys": True}, "then": "block"},
                {"name": "dropped-column", "when": {"column_removed": True}, "then": "block"},
                {"name": "null-keys", "when": {"null_keys": True}, "then": "needs_human"},
                {"name": "type-change", "when": {"column_type_changed": True}, "then": "needs_human"},
                {"name": "row-loss", "when": {"removed_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "row-count-drop", "when": {"row_count_drop_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "many-rows-changed", "when": {"changed_pct_gt": 5}, "then": "needs_human"},
                {"name": "numeric-total-moved", "when": {"sum_change_pct_gt": 1}, "then": "needs_human"},
                {"name": "pii-table-changed", "when": {"tag": "pii", "any_change": True}, "then": "needs_human"},
                {"name": "certified-downstream", "when": {"downstream_tag": "certified", "any_change": True}, "then": "needs_human"},
                {"name": "wide-blast-radius", "when": {"downstream_count_gt": 10}, "then": "needs_human"},
            ],
        },
    },
    "pii": {
        "description": "Tables holding personal data: any change to a PII table, or feeding one, needs a human.",
        "policy": {
            "default": "pass",
            "notify_owners_on": "needs_human",
            "rules": _CORE + [
                {"name": "lineage-unavailable", "when": {"lineage_error": True}, "then": "needs_human"},
                {"name": "pii-table-changed", "when": {"tag": "pii", "any_change": True}, "then": "needs_human"},
                {"name": "pii-new-column", "when": {"tag": "pii", "column_added": True}, "then": "needs_human"},
                {"name": "pii-downstream", "when": {"downstream_tag": "pii", "any_change": True}, "then": "needs_human"},
                {"name": "row-loss", "when": {"removed_pct_gt": 1}, "then": "needs_human"},
            ],
        },
    },
    "certified": {
        "description": "Certified or gold tables that dashboards and AI rely on: protect them and everything that feeds them.",
        "policy": {
            "default": "pass",
            "notify_owners_on": "needs_human",
            "rules": _CORE + [
                {"name": "lineage-unavailable", "when": {"lineage_error": True}, "then": "needs_human"},
                {"name": "certified-table-changed", "when": {"tag": "certified", "any_change": True}, "then": "needs_human"},
                {"name": "certified-schema-change", "when": {"tag": "certified", "column_type_changed": True}, "then": "block"},
                {"name": "certified-downstream", "when": {"downstream_tag": "certified", "any_change": True}, "then": "needs_human"},
                {"name": "dashboard-downstream", "when": {"downstream_kind": "dashboard", "any_change": True}, "then": "warn"},
                {"name": "row-count-drop", "when": {"row_count_drop_pct_gt": 0.5}, "then": "needs_human"},
            ],
        },
    },
    "finance": {
        "description": "Revenue and other money tables: totals must not move quietly, and double counting is blocked.",
        "policy": {
            "default": "pass",
            "notify_owners_on": "needs_human",
            "rules": _CORE + [
                {"name": "lineage-unavailable", "when": {"lineage_error": True}, "then": "warn"},
                {"name": "numeric-total-moved", "when": {"sum_change_pct_gt": 0.5}, "then": "needs_human"},
                {"name": "row-loss", "when": {"removed_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "row-count-drop", "when": {"row_count_drop_pct_gt": 0.1}, "then": "needs_human"},
                {"name": "new-nulls", "when": {"null_increase_gt": 0}, "then": "warn"},
                {"name": "dashboard-downstream", "when": {"downstream_kind": "dashboard", "any_change": True}, "then": "warn"},
            ],
        },
    },
}


def get_template(name: str) -> dict[str, Any]:
    """Return a copy of the named policy, ready for ``Policy(...)``."""
    key = name.strip().lower()
    if key not in TEMPLATES:
        raise KeyError(f"Unknown policy template {name!r}. Available: {', '.join(sorted(TEMPLATES))}")
    return copy.deepcopy(TEMPLATES[key]["policy"])


def to_yaml(name: str) -> str:
    """The template as an editable policy.yml, one rule per line."""
    p = get_template(name)
    lines = [
        f"# bricksentry policy template: {name}",
        f"# {TEMPLATES[name.strip().lower()]['description']}",
        f"default: {p.get('default', 'pass')}",
        f"notify_owners_on: {p.get('notify_owners_on', 'needs_human')}",
        "rules:",
    ]
    width = max(len(r["name"]) for r in p["rules"]) + 1
    for r in p["rules"]:
        when = ", ".join(f"{k}: {_scalar(v)}" for k, v in r["when"].items())
        lines.append(f"  - {{name: {(r['name'] + ',').ljust(width)} when: {{{when}}}, then: {r['then']}}}")
    return "\n".join(lines) + "\n"


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
