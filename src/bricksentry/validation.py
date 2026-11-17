"""A reproducible validation battery: the risky changes bricksentry is expected to catch.

Run it on your own workspace to see the same results we publish in VALIDATION.md::

    from bricksentry.validation import run_validation, print_results
    results = run_validation(backend, scratch="main.bricksentry_validation")
    print_results(results)

Everything happens in ``scratch``: an HR-style employees table and a department mart
are generated there, each scenario writes a candidate table next to them, and every
table is dropped at the end. Nothing outside ``scratch`` is read or written.

Lineage and tags are fixed in code (the mart is ``certified`` and reads the employees
table, which is ``pii``), so the verdicts don't depend on your workspace's lineage
history. Unity Catalog lineage itself is exercised by the check-my-table notebook.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from bricksentry.assess import Sentry
from bricksentry.backends.base import Backend
from bricksentry.config import Config
from bricksentry.lineage import StaticLineage
from bricksentry.models import Verdict
from bricksentry.policy import Policy
from bricksentry.policy_templates import get_template

_NAME = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?")
DEPARTMENTS = ["Sales", "Engineering", "Finance", "Marketing", "Support", "HR", "Legal", "Operations"]


@dataclass
class ScenarioResult:
    name: str
    attack: str
    expected: str
    actual: str
    passed: bool | None  # None: skipped (e.g. needs Delta history)
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"scenario": self.name, "attack": self.attack, "expected": self.expected,
                "actual": self.actual, "result": _status(self.passed), "seconds": round(self.seconds, 2),
                **self.details}


def _status(p: bool | None) -> str:
    return "SKIP" if p is None else ("PASS" if p else "FAIL")


def run_validation(
    backend: Backend,
    scratch: str,
    rows: int = 200_000,
    exact_rows: int = 1_000_000,
    keep_tables: bool = False,
) -> list[ScenarioResult]:
    """Run every scenario in ``scratch`` (``catalog.schema`` or ``schema``) and return
    the results. ``rows`` sizes the employees table; ``exact_rows`` the exact-totals table."""
    if not _NAME.fullmatch(scratch):
        raise ValueError(f"scratch must be schema or catalog.schema; got {scratch!r}")
    v = _Validation(backend, scratch, rows, exact_rows)
    try:
        v.setup()
        return v.run_all()
    finally:
        if not keep_tables:
            v.cleanup()


def print_results(results: list[ScenarioResult], out=print) -> None:
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)
    for r in results:
        out(f"[{_status(r.passed)}] {r.name} ({r.seconds:.1f}s)")
        out(f"    attack:   {r.attack}")
        out(f"    expected: {r.expected}")
        out(f"    actual:   {r.actual}")
    out("")
    out(f"{passed} passed, {failed} failed, {skipped} skipped")


class _Validation:
    def __init__(self, backend: Backend, scratch: str, rows: int, exact_rows: int):
        self.db = backend
        self.s = scratch
        self.rows = rows
        self.exact_rows = exact_rows
        self.made: list[str] = []
        self.emp = self.t("bsv_employees")
        self.mart = self.t("bsv_dept_mart")
        lineage = StaticLineage(
            edges={self.emp: [self.mart, "notebook:/Shared/hr_report"]},
            owners={self.mart: "hr-analytics"},
            tags={self.mart: {"certified": "true"}, self.emp: {"pii": "true"}},
        )
        cfg = Config.from_dict({
            "assessment_store": False,
            "sandbox_schema": scratch,
            "tables": {self.emp: {"keys": ["emp_id"]}, self.mart: {"keys": ["department"]}},
        })
        self.sentry = Sentry(cfg, backend=backend, lineage=lineage)
        self.certified = Sentry(cfg, backend=backend, lineage=lineage, policy=Policy(get_template("certified")))

    # --- helpers --------------------------------------------------------------
    def t(self, name: str) -> str:
        return f"{self.s}.{name}"

    def q(self, name: str) -> str:
        return self.db.qualified(name)

    def ids(self, n: int) -> str:
        if self.db.name == "duckdb":
            return f"SELECT i FROM range(1, {int(n) + 1}) t(i)"
        return f"SELECT id AS i FROM range(1, {int(n) + 1})"

    def create(self, name: str, select: str) -> str:
        full = self.t(name)
        self.db.execute(f"DROP TABLE IF EXISTS {self.q(full)}")
        self.db.execute(f"CREATE TABLE {self.q(full)} AS {select}")
        self.made.append(full)
        return full

    def setup(self) -> None:
        self.db.ensure_schema(self.s)
        dept = "CASE " + " ".join(f"WHEN i % 9 = {k} THEN '{d}'" for k, d in enumerate(DEPARTMENTS)) + " END"
        self.create("bsv_employees",
                    f"SELECT i AS emp_id, {dept} AS department, "
                    f"CAST(40000 + (i % 1000) * 50 AS DECIMAL(12, 2)) AS salary, "
                    f"CONCAT('user', CAST(i AS STRING), '@example.com') AS email FROM ({self.ids(self.rows)}) ids"
                    .replace("AS STRING", f"AS {self.db.string_type}"))
        self.create("bsv_dept_mart",
                    f"SELECT department, COUNT(*) AS headcount, CAST(SUM(salary) AS DECIMAL(18, 2)) AS total_salary, "
                    f"CAST(AVG(salary) AS DECIMAL(12, 2)) AS avg_salary FROM {self.q(self.emp)} GROUP BY department")

    def cleanup(self) -> None:
        for name in reversed(self.made):
            try:
                self.db.drop(name)
            except Exception:  # pragma: no cover - best effort
                pass

    def assess(self, table: str, candidate: str, sentry: Sentry | None = None, sample_pct: float | None = None):
        s = sentry or self.sentry
        if sample_pct:
            d = s.diff(table, candidate, table=table, sample_pct=sample_pct)
            return s.assess([table], diffs={table: d})
        return s.assess([table], candidates={table: candidate})

    def run_all(self) -> list[ScenarioResult]:
        out = []
        for fn in (self.row_loss, self.bad_merge, self.everything_wrong, self.hidden_in_sample,
                   self.certified_totals_default, self.certified_totals_strict, self.null_keys,
                   self.null_keys_changed,
                   self.exact_totals, self.landed_change, self.typo_table):
            t0 = time.time()
            try:
                r = fn()
            except Exception as e:  # a scenario that errors is a failure, not a crash
                r = ScenarioResult(fn.__name__, "", "runs without error", f"error: {str(e).strip()[:300]}", False)
            r.seconds = time.time() - t0
            out.append(r)
        return out

    # --- scenarios --------------------------------------------------------------
    def row_loss(self) -> ScenarioResult:
        cand = self.create("bsv_c_row_loss", f"SELECT * FROM {self.q(self.mart)} WHERE department IS NOT NULL")
        a = self.assess(self.mart, cand)
        rules = {h.rule for h in a.hits}
        ok = a.verdict.rank >= Verdict.NEEDS_HUMAN.rank and "row-loss" in rules
        return ScenarioResult("row_loss", "delete the aggregate row for employees with no department",
                              "needs_human or worse, row-loss fires", f"{a.verdict.value}: {sorted(rules)}", ok)

    def bad_merge(self) -> ScenarioResult:
        cand = self.create("bsv_c_bad_merge", f"SELECT * FROM {self.q(self.mart)} UNION ALL "
                                              f"SELECT * FROM {self.q(self.mart)} WHERE department = 'Sales'")
        a = self.assess(self.mart, cand)
        d = a.tables[0].diff
        ok = a.verdict == Verdict.BLOCK and d.added_rows == 1 and d.duplicate_keys_candidate == 1
        return ScenarioResult("bad_merge", "a MERGE re-inserts a whole department",
                              "block (duplicate-keys); the extra copy counts as 1 added row",
                              f"{a.verdict.value}, added={d.added_rows}, duplicate keys={d.duplicate_keys_candidate}", ok)

    def everything_wrong(self) -> ScenarioResult:
        st = self.db.string_type
        cand = self.create(
            "bsv_c_everything",
            f"SELECT department, CAST(headcount AS DOUBLE) AS headcount, CAST(0 AS DECIMAL(18, 2)) AS total_salary "
            f"FROM {self.q(self.mart)} WHERE department IS NULL OR department <> 'Finance' "
            f"UNION ALL SELECT department, CAST(headcount AS DOUBLE), CAST(0 AS DECIMAL(18, 2)) "
            f"FROM {self.q(self.mart)} WHERE department = 'Sales' "
            f"UNION ALL SELECT CAST(NULL AS {st}), CAST(1 AS DOUBLE), CAST(0 AS DECIMAL(18, 2))")
        a = self.assess(self.mart, cand)
        rules = {h.rule for h in a.hits}
        want = {"duplicate-keys", "dropped-column", "row-loss", "type-change", "numeric-total-moved"}
        ok = a.verdict == Verdict.BLOCK and want <= rules
        return ScenarioResult("everything_wrong",
                              "one change: drop a department, duplicate one, add a NULL key, drop a column, "
                              "change a type, zero the totals",
                              f"block, with at least {sorted(want)}", f"{a.verdict.value}: {len(rules)} rules {sorted(rules)}", ok)

    def hidden_in_sample(self) -> ScenarioResult:
        pred = self.db.sample_predicate(["emp_id"], 5)
        picked = [int(r["emp_id"]) for r in self.db.query(
            f"SELECT emp_id FROM {self.q(self.emp)} WHERE NOT ({pred}) ORDER BY emp_id LIMIT 5")]
        ids = ", ".join(map(str, picked))
        cand = self.create("bsv_c_hidden", f"SELECT emp_id, department, CASE WHEN emp_id IN ({ids}) "
                                           f"THEN CAST(salary * 5 AS DECIMAL(12, 2)) ELSE salary END AS salary, email "
                                           f"FROM {self.q(self.emp)}")
        a = self.assess(self.emp, cand, sample_pct=5)
        d = a.tables[0].diff
        ok = d.changed_rows == 5 and bool(d.sample_note) and a.verdict.rank >= Verdict.NEEDS_HUMAN.rank
        return ScenarioResult("hidden_in_sample", f"multiply 5 of {self.rows:,} salaries by 5, all outside a 5% sample",
                              "the full-table checksum catches it: 5 changed rows, needs_human or worse",
                              f"{a.verdict.value}, changed={d.changed_rows}, re-ran on all rows={bool(d.sample_note)}", ok)

    def certified_totals_default(self) -> ScenarioResult:
        cand = self._double_sales("bsv_c_cert_default")
        a = self.assess(self.mart, cand)
        ok = a.verdict == Verdict.WARN
        return ScenarioResult("certified_totals_default", "double the Sales totals in the certified mart (default policy)",
                              "warn only: the default policy is lenient on a certified table itself (documented)",
                              f"{a.verdict.value}: {sorted({h.rule for h in a.hits})}", ok)

    def certified_totals_strict(self) -> ScenarioResult:
        cand = self._double_sales("bsv_c_cert_strict")
        a = self.assess(self.mart, cand, sentry=self.certified)
        rules = {h.rule for h in a.hits}
        ok = a.verdict.rank >= Verdict.NEEDS_HUMAN.rank and "certified-table-changed" in rules and not (
            a.tables[0].diff and a.tables[0].diff.schema.type_changed)
        return ScenarioResult("certified_totals_strict", "the same change under policy builtin:certified",
                              "needs_human or worse from certified-table-changed (same column types)",
                              f"{a.verdict.value}: {sorted(rules)}", ok)

    def _double_sales(self, name: str) -> str:
        return self.create(name, f"SELECT department, headcount, CASE WHEN department = 'Sales' "
                                 f"THEN CAST(total_salary * 2 AS DECIMAL(18, 2)) ELSE total_salary END AS total_salary, avg_salary "
                                 f"FROM {self.q(self.mart)}")

    def null_keys(self) -> ScenarioResult:
        base = self.create("bsv_nulls", f"SELECT CASE WHEN i <= 2000 THEN NULL ELSE i END AS id, i AS v "
                                        f"FROM ({self.ids(3000)}) ids")
        cand = self.create("bsv_c_nulls", f"SELECT * FROM {self.q(base)}")
        d = self.sentry.diff(base, cand, keys=["id"], table=base)
        ok = (d.added_rows, d.removed_rows, d.changed_rows, d.unchanged_rows) == (0, 0, 0, 3000) and d.is_identical
        return ScenarioResult("null_keys", "2,000 rows with a NULL key on each side of an unchanged table",
                              "matched one to one: 3,000 unchanged, nothing added or removed",
                              f"unchanged={d.unchanged_rows}, added={d.added_rows}, removed={d.removed_rows}", ok)

    def null_keys_changed(self) -> ScenarioResult:
        base = self.create("bsv_nulls2", f"SELECT CASE WHEN i <= 2000 THEN NULL ELSE i END AS id, i AS v "
                                         f"FROM ({self.ids(3000)}) ids")
        cand = self.create("bsv_c_nulls2", f"SELECT id, CASE WHEN v IN (10, 1500) THEN v + 1000000 ELSE v END AS v "
                                           f"FROM {self.q(base)}")
        d = self.sentry.diff(base, cand, keys=["id"], table=base)
        ok = (d.added_rows, d.removed_rows, d.changed_rows) == (0, 0, 2)
        return ScenarioResult("null_keys_changed", "2 of 2,000 rows sharing a NULL key get a new value",
                              "exactly 2 changed; the other copies still match",
                              f"changed={d.changed_rows}, added={d.added_rows}, removed={d.removed_rows}", ok)

    def exact_totals(self) -> ScenarioResult:
        base = self.create("bsv_exact", f"SELECT i AS id, CAST(0.10 AS DECIMAL(18, 2)) AS amount "
                                        f"FROM ({self.ids(self.exact_rows)}) ids")
        cand = self.create("bsv_c_exact", f"SELECT * FROM {self.q(base)}")
        d = self.sentry.diff(base, cand, keys=["id"], table=base)
        want = round(self.exact_rows * 0.10, 2)
        c = d.columns[0]
        ok = c.base_sum == want and c.candidate_sum == want and c.sum_change_pct == 0
        return ScenarioResult("exact_totals", f"add 0.10 {self.exact_rows:,} times (DECIMAL)",
                              f"exactly {want:,}", f"base={c.base_sum!r}, candidate={c.candidate_sum!r}", ok)

    def landed_change(self) -> ScenarioResult:
        attack = "a bad MERGE is committed to a Delta table; check-last-change runs afterwards"
        if not self.db.supports_time_travel:
            return ScenarioResult("landed_change", attack, "block + RESTORE", "skipped: needs Delta history", None)
        tbl = self.create("bsv_landed", f"SELECT * FROM {self.q(self.mart)}")
        self.db.execute(f"INSERT INTO {self.q(tbl)} SELECT * FROM {self.q(tbl)} WHERE department = 'Sales'")
        a, change = self.sentry.check_change(tbl, keys=["department"])
        t = a.tables[0]
        ok = a.verdict == Verdict.BLOCK and (t.rollback or "").startswith("RESTORE") and t.diff.added_rows == 1
        return ScenarioResult("landed_change", attack, "block, 1 added row, and the RESTORE to undo it",
                              f"{a.verdict.value}, added={t.diff.added_rows}, rollback={t.rollback}", ok)

    def typo_table(self) -> ScenarioResult:
        attack = "check-last-change on a mistyped table name"
        if not self.db.supports_time_travel:
            return ScenarioResult("typo_table", attack, "one-line 'not found'", "skipped: needs Delta history", None)
        name = self.t("bsv_no_such_tabel")
        try:
            self.sentry.check_change(name, keys=["department"])
        except ValueError as e:
            msg = str(e)
            ok = msg.startswith(f"Table {name} not found.") and "\n" not in msg
            return ScenarioResult("typo_table", attack, "one clear line: 'Table … not found'", msg, ok)
        return ScenarioResult("typo_table", attack, "one clear line: 'Table … not found'", "no error raised", False)
