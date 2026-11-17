"""Project configuration, loaded from ``bricksentry.yml``.

Example::

    backend:
      type: databricks            # or duckdb
      warehouse_id: abc123        # env DATABRICKS_WAREHOUSE_ID also works
    sandbox_schema: main.bricksentry_sandbox
    candidate_pattern: "{catalog}.{schema}_pr_{pr}.{table}"
    sample_pct: 10                # diff a deterministic 10% hash sample (omit for full diff)
    sample_guard: true            # re-run on all rows if the sample misses a change (default)
    assessment_store: ~/.bricksentry/assessments   # where assessments are saved; false to keep them in memory only
    lineage:
      depth: 3
      lookback_days: 90
    tables:
      main.sales.orders:
        keys: [order_id]
        segment_by: [region]
        measures: [amount, quantity]   # totals worth checking (default: numeric columns that aren't IDs, years, ratings...)
        paths: ["pipelines/orders/**", "resources/orders_job.yml"]
    policy: policy.yml            # a file, an inline mapping, or builtin:<name> (see `bricksentry policies`)
    notify:                       # optional Slack / Teams alerts
      on: needs_human
      slack_webhook: env:SLACK_WEBHOOK_URL
      teams_webhook: env:TEAMS_WEBHOOK_URL
"""

from __future__ import annotations

import difflib
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAMES = ("bricksentry.yml", "bricksentry.yaml", ".bricksentry.yml")

TOP_LEVEL_KEYS = {
    "backend", "sandbox_schema", "candidate_pattern", "sample_pct", "sample_guard",
    "assessment_store", "max_samples", "lineage", "tables", "policy", "notify", "agent_tables", "float_tolerance",
}
TABLE_KEYS = {"keys", "key", "segment_by", "paths", "candidate", "measures"}
NOTIFY_KEYS = {"on", "slack_webhook", "teams_webhook", "mentions", "run_url"}
LINEAGE_KEYS = {
    "type", "depth", "lookback_days", "lineage_table", "tables_table", "tags_table", "max_nodes",
    "exclude", "exclude_dropped", "edges", "owners", "tags",
    "column_depth", "column_lineage_table", "column_tags_table", "column_edges", "column_tags",
}


def _agent_tables(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("agent_tables must be a list of table names or patterns, e.g. ['main.sales_dev.*']")
    return list(v)


def _tolerance(v: Any) -> float | None:
    if v in (None, False, 0):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"float_tolerance must be a number like 1e-9, not {v!r}") from None
    if not 0 < t < 1:
        raise ValueError(f"float_tolerance must be between 0 and 1 (a relative tolerance like 1e-9), not {v!r}")
    return t


def _unknown(where: str, given: Any, known: set[str]) -> list[str]:
    """Warnings for keys bricksentry would silently ignore (usually a typo)."""
    if not isinstance(given, dict):
        return []
    out = []
    for k in given:
        if k in known:
            continue
        near = difflib.get_close_matches(str(k), sorted(known), n=1, cutoff=0.6)
        out.append(f"{where}: unknown key {k!r} is ignored" + (f" (did you mean {near[0]!r}?)" if near else ""))
    return out


def config_warnings(data: dict[str, Any]) -> list[str]:
    """Typos and misplaced keys in a bricksentry.yml mapping. Never raises."""
    if not isinstance(data, dict):
        return ["bricksentry.yml should be a mapping of settings"]
    w = _unknown("bricksentry.yml", data, TOP_LEVEL_KEYS)
    w += _unknown("notify", _notify(data.get("notify")), NOTIFY_KEYS)
    w += _unknown("lineage", data.get("lineage"), LINEAGE_KEYS)
    for name, spec in (data.get("tables") or {}).items() if isinstance(data.get("tables"), dict) else []:
        w += _unknown(f"tables.{name}", spec, TABLE_KEYS)
        if isinstance(spec, dict) and not (spec.get("keys") or spec.get("key")):
            w.append(f"tables.{name}: no keys set, so rows can't be matched (diffs will need --key)")
        if str(name).count(".") != 2 and str((data.get("backend") or {}).get("type", "")).lower() == "databricks":
            w.append(f"tables.{name}: use the full catalog.schema.table name")
    n = data.get("notify")
    if isinstance(n, dict):
        for k in ("slack_webhook", "teams_webhook"):
            v = str(n.get(k) or "")
            if v.startswith("https://"):
                w.append(f"notify.{k}: the webhook URL is written in the file; it is a secret, use env:NAME instead")
    return w


def _notify(n: Any) -> dict[str, Any]:
    """YAML 1.1 reads an unquoted ``on:`` key as the boolean True; map it back to "on"."""
    if not isinstance(n, dict):
        return n or {}
    return {("on" if k is True else k): v for k, v in n.items()}


def _measures(v: Any) -> list[str] | str | None:
    if v is None or v == []:
        return None
    if isinstance(v, str):
        return "all" if v.strip().lower() == "all" else [v]
    return [str(x) for x in v]


def _load_policy_file(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if not path.exists():
        from bricksentry.policy_templates import TEMPLATES

        hint = ""
        if name.strip().lower() in TEMPLATES:
            hint = f" To use the built-in template, write `policy: builtin:{name.strip().lower()}`."
        raise FileNotFoundError(f"Policy file {name!r} not found (looked for {path}).{hint}")
    return yaml.safe_load(path.read_text()) or {}


@dataclass
class TableSpec:
    name: str
    keys: list[str]
    segment_by: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    candidate: str | None = None  # explicit candidate table, overrides candidate_pattern
    measures: list[str] | str | None = None  # columns whose totals matter; "all" for every numeric column


@dataclass
class Config:
    backend: dict[str, Any] = field(default_factory=lambda: {"type": "duckdb"})
    sandbox_schema: str = "bricksentry_sandbox"
    candidate_pattern: str = "{catalog}.{schema}_ci.{table}"
    sample_pct: float | None = None
    sample_guard: bool = True
    assessment_store: str | bool | None = None
    max_samples: int = 5
    lineage: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, TableSpec] = field(default_factory=dict)
    policy: dict[str, Any] | None = None
    notify: dict[str, Any] = field(default_factory=dict)
    agent_tables: list[str] | None = None  # tables an agent may assess or preview (globs); None = all
    float_tolerance: float | None = None  # opt-in relative tolerance for FLOAT/DOUBLE compares
    root: Path = field(default_factory=Path.cwd)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], root: Path | None = None) -> Config:
        root = root or Path.cwd()
        tables = {}
        for name, spec in (data.get("tables") or {}).items():
            spec = spec or {}
            keys = spec.get("keys") or spec.get("key") or []
            if isinstance(keys, str):
                keys = [keys]
            seg = spec.get("segment_by") or []
            if isinstance(seg, str):
                seg = [seg]
            tables[name] = TableSpec(
                name=name,
                keys=list(keys),
                segment_by=list(seg),
                paths=list(spec.get("paths") or []),
                candidate=spec.get("candidate"),
                measures=_measures(spec.get("measures")),
            )
        policy = data.get("policy")
        if isinstance(policy, str):
            if policy.strip().lower().startswith("builtin:"):
                from bricksentry.policy_templates import get_template

                policy = get_template(policy.split(":", 1)[1])
            else:
                policy = _load_policy_file(root, policy)
        return cls(
            backend=data.get("backend") or {"type": "duckdb"},
            sandbox_schema=data.get("sandbox_schema", "bricksentry_sandbox"),
            candidate_pattern=data.get("candidate_pattern", "{catalog}.{schema}_ci.{table}"),
            sample_pct=data.get("sample_pct"),
            sample_guard=bool(data.get("sample_guard", True)),
            assessment_store=data.get("assessment_store"),
            max_samples=int(data.get("max_samples", 5)),
            lineage=data.get("lineage") or {},
            tables=tables,
            policy=policy,
            notify=_notify(data.get("notify")),
            agent_tables=_agent_tables(data.get("agent_tables")),
            float_tolerance=_tolerance(data.get("float_tolerance")),
            root=root,
            warnings=config_warnings(data),
        )

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, warn: bool = True) -> Config:
        """Load bricksentry.yml. Typos and ignored keys are reported as Python warnings
        (``warn=False`` to skip that and read ``config.warnings`` yourself)."""
        cfg = cls._load(path)
        if warn:
            for w in cfg.warnings:
                warnings.warn(f"bricksentry config: {w}", UserWarning, stacklevel=2)
        return cfg

    @classmethod
    def _load(cls, path: str | os.PathLike | None = None) -> Config:
        if path is None:
            path = os.environ.get("BRICKSENTRY_CONFIG")
        if path is None:
            for name in DEFAULT_CONFIG_NAMES:
                if Path(name).exists():
                    path = name
                    break
        if path is None:
            return cls()
        p = Path(path)
        data = yaml.safe_load(p.read_text()) or {}
        return cls.from_dict(data, root=p.resolve().parent)

    def store_dir(self) -> Path | None:
        """Folder where assessments are saved as JSON, or None to keep them in memory."""
        v = self.assessment_store
        if v is False:
            return None
        if v in (None, True, ""):
            v = os.environ.get("BRICKSENTRY_STORE") or "~/.bricksentry/assessments"
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def spec(self, table: str) -> TableSpec:
        if table in self.tables:
            return self.tables[table]
        return TableSpec(name=table, keys=[])

    def candidate_for(self, table: str, pr: str | None = None) -> str:
        spec = self.spec(table)
        if spec.candidate:
            return spec.candidate.format(pr=pr or os.environ.get("BRICKSENTRY_PR", "local"))
        parts = table.split(".")
        while len(parts) < 3:
            parts.insert(0, "")
        catalog, schema, name = parts[-3], parts[-2], parts[-1]
        out = self.candidate_pattern.format(
            catalog=catalog,
            schema=schema,
            table=name,
            pr=pr or os.environ.get("BRICKSENTRY_PR", "local"),
        )
        return out.lstrip(".")
