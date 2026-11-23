"""The ``Sentry``: one object that the CLI, the MCP server and Python users all share."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from bricksentry import governed
from bricksentry.backends.base import Backend, make_backend
from bricksentry.config import Config
from bricksentry.diff import diff_tables
from bricksentry.explain import explain_columns, explain_diff, explain_impact
from bricksentry.lineage import LineageProvider, make_lineage
from bricksentry.models import Assessment, ImpactNode, TableAssessment, TableDiff, Verdict
from bricksentry.policy import ColumnContext, Policy
from bricksentry.preview import rewrite_for_preview, sandbox_name, statement_head


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
            float_tolerance=self.config.float_tolerance,
        )
        self._diffs[d.table] = d
        return d

    def downstream(self, table: str, depth: int | None = None) -> list[ImpactNode]:
        """Downstream lineage with owners and tags read afresh (see ``LineageProvider.refresh``)."""
        self.lineage.refresh()
        return self._downstream(table, depth)

    def _downstream(self, table: str, depth: int | None = None) -> list[ImpactNode]:
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
        # Governance metadata (tags, owners) is read afresh for every check, never reused from an
        # earlier one: in a long-lived process a tag added since would otherwise be missed.
        self.lineage.refresh()
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
                nodes = self._downstream(table)
            except Exception as e:  # e.g. no SELECT on system.access.table_lineage
                nodes = []
                lineage_err = _first_line(e)
            owner, tags = self.lineage.owner_and_tags(table)
            measures = self.config.spec(table).measures
            cols, col_note = self._column_context(table, diff)
            verdict, hits = self.policy.evaluate(
                table, diff, nodes, tags, diff_error=err, measures=measures, lineage_error=lineage_err, columns=cols
            )
            summary = (explain_diff(diff, measures) if diff else f"{table}: diff unavailable ({err}).") + " " + (
                f"{table}: blast radius unknown, lineage lookup failed ({lineage_err})."
                if lineage_err else explain_impact(table, nodes)
            ) + explain_columns(cols, col_note)
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
                changed_columns=cols.changed,
                column_tags={c: cols.tags[c] for c in cols.changed if cols.tags.get(c)},
                column_downstream=cols.downstream,
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
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            bricksentry_version=_version(),
            policy_sha256=self.policy.sha256,
            triggered_by=_actor(),
        )
        self._remember(a)
        self._save(a)
        return a

    def _column_context(self, table: str, diff: TableDiff | None) -> tuple[ColumnContext, str | None]:
        """Which columns changed, their tags, and the downstream columns they feed.
        Lookups that fail leave that part empty and return a one-line note."""
        changed = changed_columns(diff)
        if not changed:
            return ColumnContext(), None
        notes: list[str] = []
        col_tags: dict[str, dict[str, str]] = {}
        feeds: list = []
        try:
            col_tags = self.lineage.column_tags(table)
        except Exception as e:  # e.g. no access to information_schema.column_tags
            notes.append(f"column tags unavailable ({_first_line(e)})")
        try:
            depth = int(self.config.lineage.get("column_depth", 2))
            if depth > 0:
                feeds = self.lineage.column_downstream(table, changed, depth)
        except Exception as e:  # e.g. no SELECT on system.access.column_lineage
            notes.append(f"column lineage unavailable ({_first_line(e)})")
        return ColumnContext(changed, col_tags, feeds), "; ".join(notes) or None

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
        self._remember(a)
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
        what = governed.describe(self.backend, target)
        if what:
            raise ValueError(governed.preview_message(target, what))
        schema = ".".join(clone.split(".")[:-1])
        created = bool(schema and self.backend.ensure_schema(schema))
        try:
            self.backend.clone(target, clone)
        except Exception as e:
            what = governed.from_error(e)
            if what:
                raise ValueError(governed.preview_message(target, what)) from None
            raise RuntimeError(
                f"Could not clone {target} to preview the write: {_first_line(e)}. Check the table name "
                f"and that you can create tables in {schema or 'the sandbox schema'}."
            ) from None
        # CREATE OR REPLACE TABLE writes its result into a fresh sandbox table rather than
        # replacing the clone: some engines refuse to replace a shallow clone (serverless
        # applies CLUSTER BY AUTO to new tables, which a clone can't take). The clone stays,
        # for statements that read the target (CREATE OR REPLACE TABLE t AS SELECT ... FROM t).
        result = clone
        head = statement_head(rewritten)
        lead = len(rewritten) - len(head)
        m = _REPLACE_HEAD.match(head)
        if m:
            result = clone + "_out"
            rewritten = rewritten[: lead + m.start("name")] + ".".join(
                self.backend.quote(p) for p in result.split(".")) + rewritten[lead + m.end("name"):]
        try:
            try:
                self.backend.execute(rewritten)
            except Exception as e:
                raise RuntimeError(
                    f"The statement failed on the preview clone, so production was not touched: {_first_line(e)}"
                ) from None
            diff = self.diff(target, result, keys=keys, table=target)
            assessment = self.assess([target], diffs={target: diff})
        finally:
            if not keep:
                self.backend.drop(clone)
                if result != clone:
                    try:
                        self.backend.drop(result)
                    except Exception:
                        pass
        out = assessment.to_dict()
        out["preview"] = {"sandbox_table": result if keep else None, "executed_sql": rewritten}
        if created:
            out["preview"]["created_schema"] = schema
        return out

    # --- alerts ---------------------------------------------------------------
    def check_change(
        self,
        table: str,
        keys: list[str] | None = None,
        version: int | None = None,
        since: Any = None,
        sample_pct: float | None = None,
    ):
        """Assess a change that already landed in ``table`` (from its Delta history).

        Returns ``(assessment, change)``. By default the newest data change is checked; pass
        ``version`` for a specific one, or ``since`` (a timestamp) to check everything after it
        as one change. The table is only read; see :mod:`bricksentry.history`.
        """
        from bricksentry.history import check_change

        return check_change(self, table, keys=keys, version=version, since=since, sample_pct=sample_pct)

    def notify(self, assessment: Assessment, pr: str | None = None, force: bool = False) -> list[str]:
        """Send a Slack / Teams alert for ``assessment`` if configured. Never raises."""
        from bricksentry.notify import NotifySettings, send

        try:
            settings = NotifySettings.from_config(self.config.notify)
        except ValueError as e:
            return [f"notify: bad config ({e})"]
        return send(assessment, settings, pr=pr, force=force)

    _MAX_IN_MEMORY = 500  # a long-lived server shouldn't grow forever; older ones reload from the store

    def _remember(self, a: Assessment) -> None:
        self._assessments.pop(a.id, None)
        self._assessments[a.id] = a
        while len(self._assessments) > self._MAX_IN_MEMORY:
            self._assessments.pop(next(iter(self._assessments)))

    # --- lookups for agents -------------------------------------------------
    def get_assessment(self, assessment_id: str) -> Assessment | None:
        return self._assessments.get(assessment_id) or self._load(assessment_id)

    def get_diff(self, table: str) -> TableDiff | None:
        return self._diffs.get(table)


def _version() -> str:
    from bricksentry import __version__  # imported late: bricksentry/__init__ imports this module

    return __version__


def _actor() -> str | None:
    """Who asked for the check: BRICKSENTRY_ACTOR if set, else the GitHub actor in
    GitHub Actions. Nothing is read from the machine itself."""
    if os.environ.get("BRICKSENTRY_ACTOR"):
        return os.environ["BRICKSENTRY_ACTOR"].strip()[:200]
    if os.environ.get("GITHUB_ACTOR"):
        return "github:" + os.environ["GITHUB_ACTOR"].strip()[:200]
    return None


def _first_line(e: Exception, limit: int = 200) -> str:
    """A short, single-line error for verdict reasons and alerts."""
    text = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def changed_columns(diff: TableDiff | None) -> list[str]:
    """Columns a change touched: changed values, dropped or retyped, or new NULLs."""
    if diff is None or diff.error:
        return []
    out: list[str] = []
    for c in diff.columns:
        if c.changed_rows or (
            c.base_nulls is not None and c.candidate_nulls is not None and c.candidate_nulls > c.base_nulls
        ):
            out.append(c.column)
    out += list(diff.schema.removed) + list(diff.schema.type_changed)
    return list(dict.fromkeys(out))


_REPLACE_HEAD = re.compile(
    r"^\s*(?:CREATE\s+OR\s+REPLACE\s+TABLE|REPLACE\s+TABLE)\s+(?P<name>(?:`[^`]+`|\"[^\"]+\"|[\w\-]+)"
    r"(?:\.(?:`[^`]+`|\"[^\"]+\"|[\w\-]+))*)",
    re.IGNORECASE,
)


def _first_line(e: BaseException) -> str:
    """The useful part of an engine error: its first line, trimmed."""
    text = str(e).strip()
    line = text.splitlines()[0] if text else type(e).__name__
    return line if len(line) <= 300 else line[:297] + "..."
