"""Result types. Every result is a plain dataclass with a stable ``to_dict()``,
so agents can branch on fields and humans get the same data rendered as markdown."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Ordered from safest to most severe."""

    PASS = "pass"
    WARN = "warn"
    NEEDS_HUMAN = "needs_human"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return [Verdict.PASS, Verdict.WARN, Verdict.NEEDS_HUMAN, Verdict.BLOCK].index(self)

    @property
    def risk_tier(self) -> str:
        return {
            Verdict.PASS: "low",
            Verdict.WARN: "medium",
            Verdict.NEEDS_HUMAN: "high",
            Verdict.BLOCK: "blocked",
        }[self]

    @staticmethod
    def worst(verdicts: list["Verdict"]) -> "Verdict":
        return max(verdicts, key=lambda v: v.rank, default=Verdict.PASS)


@dataclass
class ColumnChange:
    column: str
    changed_rows: int
    base_nulls: int | None = None
    candidate_nulls: int | None = None
    base_sum: float | None = None
    candidate_sum: float | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sum_change_pct(self) -> float | None:
        if self.base_sum in (None, 0) or self.candidate_sum is None:
            return None
        return (self.candidate_sum - self.base_sum) / abs(self.base_sum) * 100


@dataclass
class SchemaChange:
    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    type_changed: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.type_changed)


@dataclass
class Segment:
    column: str
    value: Any
    affected_rows: int


@dataclass
class TableDiff:
    table: str
    base: str
    candidate: str
    keys: list[str]
    base_rows: int
    candidate_rows: int
    added_rows: int
    removed_rows: int
    changed_rows: int
    unchanged_rows: int
    schema: SchemaChange
    columns: list[ColumnChange] = field(default_factory=list)
    duplicate_keys_base: int = 0
    duplicate_keys_candidate: int = 0
    segments: list[Segment] = field(default_factory=list)
    sampled_pct: float | None = None
    error: str | None = None

    def _pct(self, n: int) -> float:
        denom = max(self.base_rows, 1)
        return round(n / denom * 100, 4)

    @property
    def added_pct(self) -> float:
        return self._pct(self.added_rows)

    @property
    def removed_pct(self) -> float:
        return self._pct(self.removed_rows)

    @property
    def changed_pct(self) -> float:
        return self._pct(self.changed_rows)

    @property
    def row_count_change_pct(self) -> float:
        return self._pct(self.candidate_rows - self.base_rows)

    @property
    def is_identical(self) -> bool:
        return (
            self.error is None
            and self.schema.is_empty
            and self.added_rows == 0
            and self.removed_rows == 0
            and self.changed_rows == 0
            and self.base_rows == self.candidate_rows
            and self.duplicate_keys_candidate <= self.duplicate_keys_base
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema"]["type_changed"] = {
            k: {"base": v[0], "candidate": v[1]} for k, v in self.schema.type_changed.items()
        }
        d["added_pct"] = self.added_pct
        d["removed_pct"] = self.removed_pct
        d["changed_pct"] = self.changed_pct
        d["row_count_change_pct"] = self.row_count_change_pct
        d["is_identical"] = self.is_identical
        for c, cc in zip(d["columns"], self.columns):
            c["sum_change_pct"] = cc.sum_change_pct
        return d


@dataclass
class ImpactNode:
    """Something downstream of a changed table: a table, job, notebook, pipeline, dashboard, query..."""

    kind: str
    name: str
    depth: int
    via: str
    owner: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleHit:
    rule: str
    verdict: Verdict
    table: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "verdict": self.verdict.value, "table": self.table, "reason": self.reason}


@dataclass
class TableAssessment:
    table: str
    diff: TableDiff | None
    downstream: list[ImpactNode]
    owner: str | None
    tags: dict[str, str]
    rollback: str | None
    verdict: Verdict = Verdict.PASS
    hits: list[RuleHit] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "verdict": self.verdict.value,
            "risk_tier": self.verdict.risk_tier,
            "owner": self.owner,
            "tags": self.tags,
            "summary": self.summary,
            "rule_hits": [h.to_dict() for h in self.hits],
            "diff": self.diff.to_dict() if self.diff else None,
            "downstream": [n.to_dict() for n in self.downstream],
            "rollback": self.rollback,
        }


@dataclass
class Assessment:
    id: str
    tables: list[TableAssessment]
    verdict: Verdict
    notify: list[str] = field(default_factory=list)

    @property
    def hits(self) -> list[RuleHit]:
        return [h for t in self.tables for h in t.hits]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "verdict": self.verdict.value,
            "risk_tier": self.verdict.risk_tier,
            "safe_to_ship": self.verdict in (Verdict.PASS, Verdict.WARN),
            "notify": self.notify,
            "rule_hits": [h.to_dict() for h in self.hits],
            "tables": [t.to_dict() for t in self.tables],
        }
