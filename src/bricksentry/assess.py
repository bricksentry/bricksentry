"""The ``Sentry``: one object that the CLI, the MCP server and Python users all share."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from bricksentry.backends.base import Backend, make_backend
from bricksentry.config import Config
from bricksentry.diff import diff_tables
from bricksentry.explain import explain_diff, explain_impact
from bricksentry.lineage import LineageProvider, make_lineage
from bricksentry.models import Assessment, ImpactNode, TableAssessment, TableDiff, Verdict
from bricksentry.policy import Policy
from bricksentry.preview import rewrite_for_preview, sandbox_name


class Sentry:
    def __init__(
        self,
        config: Config | None = None,
        backend: Backend | None = None,
        lineage: LineageProvider | None = None,
        policy: Policy | None = None,
    ):
        self.config = config or Config.load()
        self.backend = backend or make_backend(self.config.backend)
        self.lineage = lineage or make_lineage(self.config.lineage, self.backend)
        self.policy = policy or Policy(self.config.policy)
        self._assessments: dict[str, Assessment] = {}
        self._diffs: dict[str, TableDiff] = {}

    # --- building blocks --------------------------------------------------
    def diff(
        self,
        base: str,
        candidate: str,
        keys: list[str] | None = None,
        table: str | None = None,
        sample_pct: float | None = None,
    ) -> TableDiff:
        spec = self.config.spec(table or base)
        d = diff_tables(
            self.backend,
            base,
            candidate,
            keys or spec.keys,
            table=table or base,
            sample_pct=sample_pct if sample_pct is not None else self.config.sample_pct,
            segment_by=spec.segment_by,
            max_samples=self.config.max_samples,
            sample_guard=self.config.sample_guard,
        )
        self._diffs[d.table] = d
        return d

    def downstream(self, table: str, depth: int | None = None) -> list[ImpactNode]:
        return self.lineage.downstream(table, depth or int(self.config.lineage.get("depth", 3)))

    def rollback_plan(self, table: str) -> str:
        v = self.backend.current_version(table)
        if v is None:
            return (
                f"No Delta history available for {table} on this backend; "
                "take a snapshot (CREATE TABLE ... CLONE) before applying the change."
            )
        return f"RESTORE TABLE {self.backend.qualified(table)} TO VERSION AS OF {v}"

    # --- the main entry point ---------------------------------------------
    def assess(
        self,
        tables: list[str],
        candidates: dict[str, str] | None = None,
        pr: str | None = None,
        diffs: dict[str, TableDiff] | None = None,
    ) -> Assessment:
        """Assess a change touching ``tables``.

        For each table: diff base vs candidate (unless a diff is supplied),
        walk downstream lineage, evaluate policy and record a rollback plan.
        """
        candidates = candidates or {}
        diffs = diffs or {}
        results: list[TableAssessment] = []
        notify: set[str] = set()
        for table in tables:
            diff: TableDiff | None = diffs.get(table)
            err: str | None = None
            if diff is None:
                cand = candidates.get(table) or self.config.candidate_for(table, pr)
                try:
                    if not self.backend.table_exists(cand):
                        raise ValueError(f"candidate table {cand} not found (did the CI build run?)")
                    diff = self.diff(table, cand, table=table)
                except Exception as e:  # report, don't crash: an agent needs a verdict
                    err = str(e)
            lineage_err: str | None = None
            try:
                nodes = self.downstream(table)
            except Exception as e:  # e.g. no SELECT on system.access.table_lineage
                nodes = []
                lineage_err = _first_line(e)
            owner, tags = self.lineage.owner_and_tags(table)
            measures = self.config.spec(table).measures
            verdict, hits = self.policy.evaluate(
                table, diff, nodes, tags, diff_error=err, measures=measures, lineage_error=lineage_err
            )
            summary = (explain_diff(diff, measures) if diff else f"{table}: diff unavailable ({err}).") + " " + (
                f"{table}: blast radius unknown, lineage lookup failed ({lineage_err})."
                if lineage_err else explain_impact(table, nodes)
            )
            ta = TableAssessment(
                table=table,
                diff=diff,
                downstream=nodes,
                owner=owner,
                tags=tags,
                rollback=self.rollback_plan(table),
                verdict=verdict,
                hits=hits,
                summary=summary,
            )
            results.append(ta)
            if verdict.rank >= self.policy.notify_on.rank:
                if owner:
                    notify.add(owner)
                for n in nodes:
                    if n.owner and n.kind == "table":
                        notify.add(n.owner)
        a = Assessment(
            id=uuid.uuid4().hex[:12],
            tables=results,
            verdict=Verdict.worst([t.verdict for t in results]),
            notify=sorted(notify),
        )
        self._assessments[a.id] = a
        self._save(a)
        return a

    # --- saved assessments -------------------------------------------------
    def _save(self, a: Assessment) -> None:
        """Save an assessment as JSON so another Sentry (another process, the MCP
        server, a later notebook cell) can look it up by id. Best effort."""
        folder = self.config.store_dir()
        if folder is None:
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{a.id}.json").write_text(json.dumps(a.to_dict(), default=str))
        except OSError:
            pass

    def _load(self, assessment_id: str) -> Assessment | None:
        folder = self.config.store_dir()
        if folder is None or not re.fullmatch(r"[0-9a-f]{1,64}", assessment_id or ""):
            return None
        f = folder / f"{assessment_id}.json"
        try:
            a = Assessment.from_dict(json.loads(f.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self._assessments[a.id] = a
        return a

    def preview_write(
        self,
        sql: str,
        target: str,
        keys: list[str] | None = None,
        keep: bool = False,
    ) -> dict[str, Any]:
        """Run a write against a sandbox clone of ``target`` and return the assessment of its effect."""
        spec = self.config.spec(target)
        keys = keys or spec.keys
        if not keys:
            raise ValueError(f"No keys configured for {target}; pass keys=[...] to preview_write")
        clone = sandbox_name(self.config.sandbox_schema, target)
        rewritten = rewrite_for_preview(sql, target, clone, self.backend.quote)
        schema = ".".join(clone.split(".")[:-1])
        if schema:
            self.backend.ensure_schema(schema)
        self.backend.clone(target, clone)
        try:
            self.backend.execute(rewritten)
            diff = self.diff(target, clone, keys=keys, table=target)
            assessment = self.assess([target], diffs={target: diff})
        finally:
            if not keep:
                self.backend.drop(clone)
        out = assessment.to_dict()
        out["preview"] = {"sandbox_table": clone if keep else None, "executed_sql": rewritten}
        return out

    # --- alerts ---------------------------------------------------------------
    def notify(self, assessment: Assessment, pr: str | None = None, force: bool = False) -> list[str]:
        """Send a Slack / Teams alert for ``assessment`` if configured. Never raises."""
        from bricksentry.notify import NotifySettings, send

        try:
            settings = NotifySettings.from_config(self.config.notify)
        except ValueError as e:
            return [f"notify: bad config ({e})"]
        return send(assessment, settings, pr=pr, force=force)

    # --- lookups for agents -------------------------------------------------
    def get_assessment(self, assessment_id: str) -> Assessment | None:
        return self._assessments.get(assessment_id) or self._load(assessment_id)

    def get_diff(self, table: str) -> TableDiff | None:
        return self._diffs.get(table)


def _first_line(e: Exception, limit: int = 200) -> str:
    """A short, single-line error for verdict reasons and alerts."""
    text = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return text if len(text) <= limit else text[: limit - 1] + "…"
