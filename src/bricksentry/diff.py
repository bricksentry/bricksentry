"""Key-based data diff, pushed down to the engine as SQL.

Compares a *base* table (what production has now) with a *candidate* table
(what the change produces) on primary-key columns and reports:

* schema changes (added / removed / type-changed columns)
* row counts, added / removed / changed / unchanged rows
* per-column change counts, null counts and numeric sums
* duplicate keys on either side
* sample changed values per column
* segment attribution ("most changes are in region = APAC")

Sampling uses a deterministic hash on the keys, so both sides sample the same keys.
"""

from __future__ import annotations

from bricksentry.backends.base import Backend
from bricksentry.models import ColumnChange, SchemaChange, Segment, TableDiff

MAX_SAMPLE_COLUMNS = 10


class DiffError(ValueError):
    pass


def diff_tables(
    backend: Backend,
    base: str,
    candidate: str,
    keys: list[str],
    *,
    table: str | None = None,
    sample_pct: float | None = None,
    segment_by: list[str] | None = None,
    max_samples: int = 5,
    columns: list[str] | None = None,
) -> TableDiff:
    if not keys:
        raise DiffError(
            f"No key columns for {table or base}. Add `keys:` for it in bricksentry.yml "
            "(or pass --key) so rows can be matched."
        )
    q = backend.quote
    b_cols = backend.columns(base)
    c_cols = backend.columns(candidate)

    missing = [k for k in keys if k not in b_cols or k not in c_cols]
    if missing:
        raise DiffError(f"Key column(s) {missing} not present in both {base} and {candidate}")

    schema = SchemaChange(
        added={k: v for k, v in c_cols.items() if k not in b_cols},
        removed={k: v for k, v in b_cols.items() if k not in c_cols},
        type_changed={
            k: (b_cols[k], c_cols[k]) for k in b_cols if k in c_cols and b_cols[k] != c_cols[k]
        },
    )
    common = [c for c in b_cols if c in c_cols and c not in keys]
    if columns:
        common = [c for c in common if c in columns]

    # A marker column says whether a joined row exists on each side. Testing the key
    # for NULL instead would count a NULL-key row as both added and removed.
    present = q("__bs_present")

    def side(tbl: str, alias: str) -> str:
        where = f" WHERE {backend.sample_predicate(keys, sample_pct)}" if sample_pct else ""
        return f"{alias} AS (SELECT *, 1 AS {present} FROM {backend.qualified(tbl)}{where})"

    ctes = f"WITH {side(base, 'b_src')}, {side(candidate, 'c_src')}"
    # Null-safe key match: a NULL key in production matches a NULL key in the candidate.
    join_on = " AND ".join(f"b.{q(k)} IS NOT DISTINCT FROM c.{q(k)}" for k in keys)
    in_b, in_c = f"b.{present} IS NOT NULL", f"c.{present} IS NOT NULL"

    def cmp(col: str) -> str:
        bc, cc = f"b.{q(col)}", f"c.{q(col)}"
        if col in schema.type_changed:
            st = backend.string_type
            bc, cc = f"CAST({bc} AS {st})", f"CAST({cc} AS {st})"
        return f"{bc} IS DISTINCT FROM {cc}"

    both = f"{in_b} AND {in_c}"
    any_diff = " OR ".join(f"({cmp(c)})" for c in common) or "FALSE"

    selects = [
        f"SUM(CASE WHEN NOT ({in_b}) THEN 1 ELSE 0 END) AS added_rows",
        f"SUM(CASE WHEN NOT ({in_c}) THEN 1 ELSE 0 END) AS removed_rows",
        f"SUM(CASE WHEN {both} AND ({any_diff}) THEN 1 ELSE 0 END) AS changed_rows",
        f"SUM(CASE WHEN {both} AND NOT ({any_diff}) THEN 1 ELSE 0 END) AS unchanged_rows",
    ]
    for i, col in enumerate(common):
        selects.append(f"SUM(CASE WHEN {both} AND ({cmp(col)}) THEN 1 ELSE 0 END) AS chg_{i}")

    join_sql = (
        f"{ctes} SELECT {', '.join(selects)} "
        f"FROM b_src b FULL OUTER JOIN c_src c ON {join_on}"
    )
    j = backend.query(join_sql)[0]

    # Row counts, nulls, numeric sums per side.
    def profile(tbl_cte: str, cols: dict[str, str]) -> dict:
        any_null_key = " OR ".join(f"{q(k)} IS NULL" for k in keys)
        parts = ["COUNT(*) AS n_rows", f"SUM(CASE WHEN {any_null_key} THEN 1 ELSE 0 END) AS null_keys"]
        for i, col in enumerate(common):
            parts.append(f"SUM(CASE WHEN {q(col)} IS NULL THEN 1 ELSE 0 END) AS nul_{i}")
            if backend.is_numeric(cols[col]):
                parts.append(f"SUM(CAST({q(col)} AS DOUBLE)) AS sum_{i}")
        return backend.query(f"{ctes} SELECT {', '.join(parts)} FROM {tbl_cte}")[0]

    pb = profile("b_src", b_cols)
    pc = profile("c_src", c_cols)

    def dupes(tbl_cte: str) -> int:
        kc = ", ".join(q(k) for k in keys)
        sql = (
            f"{ctes} SELECT COUNT(*) AS n FROM "
            f"(SELECT {kc} FROM {tbl_cte} GROUP BY {kc} HAVING COUNT(*) > 1) d"
        )
        return int(backend.scalar(sql) or 0)

    col_changes: list[ColumnChange] = []
    for i, col in enumerate(common):
        changed = int(j.get(f"chg_{i}") or 0)
        cc = ColumnChange(
            column=col,
            changed_rows=changed,
            base_nulls=_int(pb.get(f"nul_{i}")),
            candidate_nulls=_int(pc.get(f"nul_{i}")),
            base_sum=_float(pb.get(f"sum_{i}")),
            candidate_sum=_float(pc.get(f"sum_{i}")),
        )
        col_changes.append(cc)

    # Samples for the most-changed columns.
    key_sel = ", ".join(f"COALESCE(b.{q(k)}, c.{q(k)}) AS {q(k)}" for k in keys)
    for cc in sorted(col_changes, key=lambda c: -c.changed_rows)[:MAX_SAMPLE_COLUMNS]:
        if cc.changed_rows == 0 or max_samples <= 0:
            continue
        col = cc.column
        sql = (
            f"{ctes} SELECT {key_sel}, b.{q(col)} AS base_value, c.{q(col)} AS candidate_value "
            f"FROM b_src b JOIN c_src c ON {join_on} WHERE {cmp(col)} "
            f"ORDER BY {', '.join(f'b.{q(k)}' for k in keys)} LIMIT {int(max_samples)}"
        )
        cc.samples = [_jsonable(r) for r in backend.query(sql)]

    # Segment attribution over added + removed + changed rows.
    segments: list[Segment] = []
    for seg in segment_by or []:
        if seg not in b_cols and seg not in c_cols:
            continue
        b_seg = f"b.{q(seg)}" if seg in b_cols else "NULL"
        c_seg = f"c.{q(seg)}" if seg in c_cols else "NULL"
        affected = f"(NOT ({in_b}) OR NOT ({in_c}) OR ({any_diff}))"
        sql = (
            f"{ctes} SELECT COALESCE({c_seg}, {b_seg}) AS seg_value, COUNT(*) AS n "
            f"FROM b_src b FULL OUTER JOIN c_src c ON {join_on} WHERE {affected} "
            f"GROUP BY 1 ORDER BY n DESC LIMIT 5"
        )
        for r in backend.query(sql):
            segments.append(Segment(column=seg, value=_jsonable_value(r["seg_value"]), affected_rows=int(r["n"])))

    return TableDiff(
        table=table or base,
        base=base,
        candidate=candidate,
        keys=list(keys),
        base_rows=int(pb.get("n_rows") or 0),
        candidate_rows=int(pc.get("n_rows") or 0),
        added_rows=int(j.get("added_rows") or 0),
        removed_rows=int(j.get("removed_rows") or 0),
        changed_rows=int(j.get("changed_rows") or 0),
        unchanged_rows=int(j.get("unchanged_rows") or 0),
        schema=schema,
        columns=col_changes,
        duplicate_keys_base=dupes("b_src"),
        duplicate_keys_candidate=dupes("c_src"),
        null_keys_base=int(pb.get("null_keys") or 0),
        null_keys_candidate=int(pc.get("null_keys") or 0),
        segments=segments,
        sampled_pct=sample_pct,
    )


def _int(v):
    return None if v is None else int(v)


def _float(v):
    return None if v is None else float(v)


def _jsonable_value(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _jsonable(row: dict) -> dict:
    return {k: _jsonable_value(v) for k, v in row.items()}
