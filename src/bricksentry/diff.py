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
    sample_guard: bool = True,
    float_tolerance: float | None = None,
) -> TableDiff:
    """Diff ``base`` against ``candidate``.

    With ``sample_pct``, only a hash sample of keys is compared. ``sample_guard``
    (on by default) then checks a full-table checksum: if the sample shows no
    change but the tables differ, the diff re-runs on every row, so a small change
    outside the sample can never pass as "no change".
    """
    # Pin both tables to the Delta version they have now, and read every query at that
    # version: a table rewritten while the diff runs must not mix two states into one
    # result. Tables without versions (views, DuckDB) are read as they are.
    seen = {base: backend.current_version(base), candidate: backend.current_version(candidate)}
    # Reading the schema at the pinned version doubles as the "can it time travel?"
    # probe (one query, not two); objects that refuse VERSION AS OF are read unpinned.
    pins: dict[str, int | None] = {}
    known_cols: dict[str, dict[str, str]] = {}
    for t, v in seen.items():
        pins[t] = None
        if v is not None:
            try:
                known_cols[t] = backend.columns_at(t, v, strict=True)
                pins[t] = v
            except Exception:
                pass
    # Column masks and row filters (Unity Catalog) change what bricksentry's identity
    # reads: a masked column can look unchanged while every value changed, and a row
    # filter hides rows from the counts. Neither can be verified, so both are reported.
    prot = backend.protections([base, candidate])
    masked = {c: "masked" for p in prot.values() for c in p.get("masked", [])}
    filtered = [role for role, t in (("base", base), ("candidate", candidate)) if prot.get(t, {}).get("row_filter")]
    d = _diff(backend, base, candidate, keys, table=table, sample_pct=sample_pct,
              segment_by=segment_by, max_samples=max_samples, columns=columns, pins=pins,
              float_tolerance=float_tolerance, masked=masked, known_cols=known_cols)
    if filtered:
        d.row_filtered = filtered
        d.notes.append(
            f"A row filter is set on the {' and '.join(filtered)} table: the diff only sees the rows bricksentry's "
            "identity is allowed to read, so counts may be incomplete. The check asks a human."
        )
    d = _finish(backend, d, base, candidate, keys, pins, table=table, sample_pct=sample_pct,
                segment_by=segment_by, max_samples=max_samples, columns=columns, sample_guard=sample_guard,
                float_tolerance=float_tolerance, masked=masked, known_cols=known_cols)
    _note_moves(backend, d, base, candidate, seen, pins)
    return d




def _finish(backend, d, base, candidate, keys, pins, *, table, sample_pct, segment_by, max_samples, columns,
            sample_guard, float_tolerance, masked=None, known_cols=None):
    # Re-check the whole table whenever the sample shows no row changes, even if the
    # schema changed: a retyped column can still hide changed values outside the sample.
    rows_look_unchanged = d.added_rows == 0 and d.removed_rows == 0 and d.changed_rows == 0
    if not (sample_pct and sample_guard and rows_look_unchanged):
        return d
    cols = list(d.keys) + [c.column for c in d.columns]
    try:
        full = _full_fingerprints(backend, base, candidate, cols, pins, (known_cols or {}).get(base))
    except Exception:  # can't checksum the whole table: compare every row instead
        full = ("unknown", "different")
    if full is None or full[0] == full[1]:
        return d
    full_d = _diff(backend, base, candidate, keys, table=table, sample_pct=None,
                   segment_by=segment_by, max_samples=max_samples, columns=columns, pins=pins,
                   float_tolerance=float_tolerance, masked=masked, known_cols=known_cols)
    full_d.sample_note = (
        f"The {sample_pct:g}% sample showed no row changes, but the full tables differ, "
        "so the diff was re-run on all rows."
    )
    return full_d


def _note_moves(backend: Backend, d: TableDiff, base: str, candidate: str, seen: dict, pins: dict) -> None:
    """After the diff: did either table get a new version while it ran? The numbers are
    still right for the pinned versions; a moved candidate means the result may no longer
    describe what will ship, which the policy treats as needs_human."""
    for role, tbl in (("base", base), ("candidate", candidate)):
        v = seen.get(tbl)
        if v is None:
            continue
        if role == "base" and pins.get(tbl) is not None:
            continue  # a pinned base can move freely: every query read its pinned version
        now = backend.current_version(tbl)
        if now is not None and now != v:
            d.moved_during_check.append(role)
            if pins.get(tbl) is not None:
                what = f"These results are for version {v}."
            else:  # couldn't pin: the queries may have read two versions
                what = "It couldn't be pinned to one version, so these results may mix both."
                d.moved_during_check.append("unpinned")  # the numbers themselves may be wrong
            d.notes.append(
                f"The {role} table {tbl} changed while it was being checked (version {v} -> {now}). {what}"
                + (" Re-run the check." if role == "candidate" or pins.get(tbl) is None else "")
            )
    if pins.get(base) is not None or pins.get(candidate) is not None:
        d.pinned_versions = {"base": pins.get(base), "candidate": pins.get(candidate)}


def _full_fingerprints(backend: Backend, base: str, candidate: str, cols: list[str], pins: dict | None = None,
                       types: dict[str, str] | None = None):
    """(rows, checksum) for each table over all rows, or None if unsupported."""
    agg = backend.row_checksum(cols, types or backend.columns_at(base, (pins or {}).get(base)))
    if agg is None:
        return None

    def fp(tbl: str):
        r = backend.query(f"SELECT COUNT(*) AS n, {agg} AS h FROM {backend.at_version(tbl, (pins or {}).get(tbl))}")[0]
        return int(r.get("n") or 0), str(r.get("h"))

    return fp(base), fp(candidate)


def _diff(
    backend: Backend,
    base: str,
    candidate: str,
    keys: list[str],
    *,
    table: str | None,
    sample_pct: float | None,
    segment_by: list[str] | None,
    max_samples: int,
    columns: list[str] | None,
    pins: dict | None = None,
    float_tolerance: float | None = None,
    masked: dict[str, str] | None = None,
    known_cols: dict[str, dict[str, str]] | None = None,
) -> TableDiff:
    if not keys:
        raise DiffError(
            f"No key columns for {table or base}. Add `keys:` for it in bricksentry.yml "
            "(or pass --key) so rows can be matched."
        )
    q = backend.quote
    pins = pins or {}

    def ref(tbl: str) -> str:
        return backend.at_version(tbl, pins.get(tbl))

    known_cols = known_cols or {}
    b_cols = known_cols.get(base) or backend.columns_at(base, pins.get(base))
    c_cols = known_cols.get(candidate) or backend.columns_at(candidate, pins.get(candidate))

    keys = [_resolve(k, b_cols) for k in keys]  # `ID` finds `id`, as the engine would
    missing = [k for k in keys if k not in b_cols or _resolve(k, c_cols) not in c_cols]
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

    def cmp(col: str) -> str:
        bc, cc = f"b.{q(col)}", f"c.{q(col)}"
        if col in schema.type_changed:
            st = backend.string_type
            return f"CAST({bc} AS {st}) IS DISTINCT FROM CAST({cc} AS {st})"
        t = b_cols[col]
        bc, cc = backend.canon(bc, t), backend.canon(cc, t)
        if float_tolerance and _is_float(t):
            # Opt-in: two finite floats within a relative tolerance count as equal
            # (x - x = 0 only for finite x, so NaN and infinity still compare exactly).
            close = (f"{bc} - {bc} = 0 AND {cc} - {cc} = 0 AND "
                     f"abs({bc} - {cc}) <= {float(float_tolerance)!r} * greatest(abs({bc}), abs({cc}))")
            return f"({bc} IS DISTINCT FROM {cc}) AND NOT COALESCE({close}, FALSE)"
        return f"{bc} IS DISTINCT FROM {cc}"

    # Columns the engine can't compare (a type it rejects even after canon()) are left
    # out with a note instead of failing the whole diff. Only nested types are probed,
    # and only at plan time (LIMIT 0), so ordinary tables pay nothing.
    masked_lc = {c.lower() for c in (masked or {})}
    not_compared: dict[str, str] = {c: "masked" for c in common if c.lower() in masked_lc}
    common = [c for c in common if c not in not_compared]
    complex_cols = [c for c in common if _is_complex(b_cols[c]) or _is_complex(c_cols[c])]
    if complex_cols:
        on_keys = " AND ".join(f"b.{q(k)} IS NOT DISTINCT FROM c.{q(k)}" for k in keys)

        def probe(cols: list[str]) -> bool:
            exprs = ", ".join(f"({cmp(c)}) AS p{i}" for i, c in enumerate(cols))
            try:
                backend.query(f"SELECT {exprs} FROM {ref(base)} b JOIN {ref(candidate)} c ON {on_keys} LIMIT 0")
                return True
            except Exception:
                return False

        if not probe(complex_cols):
            for c in complex_cols:
                if not probe([c]):
                    not_compared[c] = b_cols[c]
            common = [c for c in common if c not in not_compared]
    # Columns the row hash may use (it only orders copies of duplicated keys and checks
    # a sample against the whole table, so leaving a column out is safe).
    hash_cols = [c for c in b_cols if c in c_cols and c not in schema.type_changed and c not in not_compared]
    if complex_cols and hash_cols:
        h_try = backend.row_hash(hash_cols, b_cols)
        if h_try is not None:
            try:
                backend.query(f"SELECT {h_try} AS h FROM {ref(base)} LIMIT 0")
            except Exception:
                hash_cols = [c for c in hash_cols if c not in complex_cols]

    # A marker column says whether a joined row exists on each side. Testing the key
    # for NULL instead would count a NULL-key row as both added and removed.
    present = q("__bs_present")
    occ = q("__bs_occ")
    kc = ", ".join(q(k) for k in keys)

    def sample_where() -> str:
        return f" WHERE {backend.sample_predicate(keys, sample_pct)}" if sample_pct else ""

    def raw_cte(tbl: str, alias: str) -> str:
        return f"{alias} AS (SELECT *, 1 AS {present} FROM {ref(tbl)}{sample_where()})"

    # Duplicate keys first: a key that appears more than once (including many NULL
    # keys, which match each other) would multiply rows in the key join.
    raw_ctes = f"WITH {raw_cte(base, 'b_src')}, {raw_cte(candidate, 'c_src')}"

    def dupes(tbl_cte: str) -> int:
        sql = (
            f"{raw_ctes} SELECT COUNT(*) AS n FROM "
            f"(SELECT {kc} FROM {tbl_cte} GROUP BY {kc} HAVING COUNT(*) > 1) d"
        )
        return int(backend.scalar(sql) or 0)

    dup_b, dup_c = dupes("b_src"), dupes("c_src")
    notes: list[str] = []
    pair_copies = bool(dup_b or dup_c)
    if pair_copies:
        notes.append(
            "Some keys appear more than once. Identical copies of a key were matched first, then the "
            "remaining copies paired one to one (an extra copy counts as added or removed)."
        )
        st = backend.string_type
        h = backend.row_hash(hash_cols, b_cols) if hash_cols else None
        if h is None:  # no row hash: number the copies of each key in no particular order
            def side(tbl: str, alias: str) -> str:
                return (f"{alias} AS (SELECT *, 1 AS {present}, CAST(ROW_NUMBER() OVER (PARTITION BY {kc} "
                        f"ORDER BY {kc}) AS {st}) AS {occ} FROM {ref(tbl)}{sample_where()})")

            ctes = f"WITH {side(base, 'b_src')}, {side(candidate, 'c_src')}"
        else:
            # Two steps, so one changed copy can't shift the pairing of the others:
            # 1. identical copies match each other (same key, same row hash, numbered
            #    within that hash);
            # 2. only the copies left over on each side are paired, by rank within the key.
            hh, hn, hm = q("__bs_h"), q("__bs_hn"), q("__bs_m")

            def hashed(tbl: str, alias: str) -> str:
                return (f"{alias} AS (SELECT *, 1 AS {present}, {h} AS {hh}, ROW_NUMBER() OVER "
                        f"(PARTITION BY {kc}, {h} ORDER BY {kc}) AS {hn} FROM {ref(tbl)}{sample_where()})")

            def marked(src: str, other: str, alias: str) -> str:
                on = " AND ".join(f"o.{q(k)} IS NOT DISTINCT FROM s.{q(k)}" for k in keys)
                return (f"{alias} AS (SELECT s.*, CASE WHEN o.{hh} IS NULL THEN 0 ELSE 1 END AS {hm} "
                        f"FROM {src} s LEFT JOIN (SELECT {kc}, {hh}, {hn} FROM {other}) o "
                        f"ON {on} AND o.{hh} = s.{hh} AND o.{hn} = s.{hn})")

            def paired(src: str, alias: str) -> str:
                return (f"{alias} AS (SELECT *, CASE WHEN {hm} = 1 "
                        f"THEN CONCAT('m', CAST({hh} AS {st}), '_', CAST({hn} AS {st})) "
                        f"ELSE CONCAT('u', CAST(ROW_NUMBER() OVER (PARTITION BY {kc}, {hm} ORDER BY {hh}) AS {st})) "
                        f"END AS {occ} FROM {src})")

            ctes = ("WITH " + ", ".join([
                hashed(base, "b_h"), hashed(candidate, "c_h"),
                marked("b_h", "c_h", "b_m"), marked("c_h", "b_h", "c_m"),
                paired("b_m", "b_src"), paired("c_m", "c_src"),
            ]))
    else:
        ctes = raw_ctes
    # Null-safe key match: a NULL key in production matches a NULL key in the candidate.
    join_on = " AND ".join(f"b.{q(k)} IS NOT DISTINCT FROM c.{q(k)}" for k in keys)
    if pair_copies:
        join_on += f" AND b.{occ} = c.{occ}"
    in_b, in_c = f"b.{present} IS NOT NULL", f"c.{present} IS NOT NULL"

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

    # Row counts, nulls, numeric sums per side. Sums are exact for decimals and integers.
    def profile(tbl_cte: str, cols: dict[str, str], sums: str = "exact") -> dict:
        any_null_key = " OR ".join(f"{q(k)} IS NULL" for k in keys)
        parts = ["COUNT(*) AS n_rows", f"SUM(CASE WHEN {any_null_key} THEN 1 ELSE 0 END) AS null_keys"]
        for i, col in enumerate(common):
            parts.append(f"SUM(CASE WHEN {q(col)} IS NULL THEN 1 ELSE 0 END) AS nul_{i}")
            if backend.is_numeric(cols[col]):
                if sums == "exact":
                    parts.append(f"{backend.exact_sum(q(col), cols[col])} AS sum_{i}")
                elif sums == "double":
                    parts.append(f"SUM(CAST({q(col)} AS DOUBLE)) AS sum_{i}")
        return backend.query(f"{raw_ctes} SELECT {', '.join(parts)} FROM {tbl_cte}")[0]

    def safe_profile(tbl_cte: str, cols: dict[str, str]) -> dict:
        # An exact DECIMAL(38) total can overflow on huge values. Don't lose the whole
        # diff over it: fall back to approximate totals, then to no totals.
        try:
            return profile(tbl_cte, cols)
        except Exception as e:
            first = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
            try:
                r = profile(tbl_cte, cols, "double")
                msg = ("Exact totals overflowed, so column totals are approximate (added as DOUBLE). "
                       f"Engine said: {first}")
            except Exception:
                r = profile(tbl_cte, cols, "none")
                msg = f"Column totals could not be computed, so total-based rules can't fire. Engine said: {first}"
            if msg not in notes:
                notes.append(msg)
            return r

    pb = safe_profile("b_src", b_cols)
    pc = safe_profile("c_src", c_cols)

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

    masked_cols = [c for c, t in not_compared.items() if t == "masked"]
    other = {c: t for c, t in not_compared.items() if t != "masked"}
    if masked_cols:
        notes.append(f"Not compared (column mask): {', '.join(masked_cols)}. A mask can hide real changes from "
                     "bricksentry's identity, so the check asks a human.")
    if other:
        notes.append("Not compared (the engine can't compare these types): "
                     + ", ".join(f"{c} ({t})" for c, t in other.items())
                     + ". Changes in these columns can't be seen, so the check asks a human.")
    if not float_tolerance:
        noisy = [cc.column for cc in col_changes if cc.changed_rows and _is_float(b_cols.get(cc.column, ""))
                 and cc.base_sum is not None and cc.candidate_sum is not None
                 and abs(cc.candidate_sum - cc.base_sum) <= 1e-9 * max(abs(cc.base_sum), abs(cc.candidate_sum), 1e-300)]
        if noisy:
            notes.append(f"Changes in {', '.join(noisy)} leave the column total the same to 9 significant digits: "
                         "likely floating-point rounding. Set `float_tolerance: 1e-9` in bricksentry.yml to "
                         "compare floats within a tolerance.")

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
        duplicate_keys_base=dup_b,
        duplicate_keys_candidate=dup_c,
        null_keys_base=int(pb.get("null_keys") or 0),
        null_keys_candidate=int(pc.get("null_keys") or 0),
        segments=segments,
        sampled_pct=sample_pct,
        notes=notes,
        not_compared=not_compared,
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


def _is_complex(type_name: str) -> bool:
    """Nested or semi-structured types, the ones an engine may refuse to compare."""
    t = type_name.lower().strip()
    return (t.startswith(("map", "struct", "array", "list", "variant", "union", "object", "json"))
            or "<" in t or t.endswith("]"))


def _is_float(type_name: str) -> bool:
    return type_name.lower() in ("float", "double", "real", "float4", "float8", "double precision")


def _resolve(name: str, cols: dict[str, str]) -> str:
    """The column's real name, matching case-insensitively when there's exactly one match."""
    if name in cols:
        return name
    hits = [c for c in cols if c.lower() == name.lower()]
    return hits[0] if len(hits) == 1 else name
