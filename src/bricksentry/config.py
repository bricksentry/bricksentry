"""Project configuration, loaded from ``bricksentry.yml``.

Example::

    backend:
      type: databricks            # or duckdb
      warehouse_id: abc123        # env DATABRICKS_WAREHOUSE_ID also works
    sandbox_schema: main.bricksentry_sandbox
    candidate_pattern: "{catalog}.{schema}_pr_{pr}.{table}"
    sample_pct: 10                # diff a deterministic 10% hash sample (omit for full diff)
    lineage:
      depth: 3
      lookback_days: 90
    tables:
      main.sales.orders:
        keys: [order_id]
        segment_by: [region]
        paths: ["pipelines/orders/**", "resources/orders_job.yml"]
    policy: policy.yml            # or inline mapping
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAMES = ("bricksentry.yml", "bricksentry.yaml", ".bricksentry.yml")


@dataclass
class TableSpec:
    name: str
    keys: list[str]
    segment_by: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    candidate: str | None = None  # explicit candidate table, overrides candidate_pattern


@dataclass
class Config:
    backend: dict[str, Any] = field(default_factory=lambda: {"type": "duckdb"})
    sandbox_schema: str = "bricksentry_sandbox"
    candidate_pattern: str = "{catalog}.{schema}_ci.{table}"
    sample_pct: float | None = None
    max_samples: int = 5
    lineage: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, TableSpec] = field(default_factory=dict)
    policy: dict[str, Any] | None = None
    root: Path = field(default_factory=Path.cwd)

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
            )
        policy = data.get("policy")
        if isinstance(policy, str):
            policy = yaml.safe_load((root / policy).read_text()) or {}
        return cls(
            backend=data.get("backend") or {"type": "duckdb"},
            sandbox_schema=data.get("sandbox_schema", "bricksentry_sandbox"),
            candidate_pattern=data.get("candidate_pattern", "{catalog}.{schema}_ci.{table}"),
            sample_pct=data.get("sample_pct"),
            max_samples=int(data.get("max_samples", 5)),
            lineage=data.get("lineage") or {},
            tables=tables,
            policy=policy,
            root=root,
        )

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
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
