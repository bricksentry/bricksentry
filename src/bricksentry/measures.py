"""Which numeric columns are *measures*, i.e. columns where a change in the total means something.

Summing an ID, a year or a rating gives a number, but a 10% move in
``sum(established_year)`` only says that rows were removed. Those totals add
noise to verdicts and alerts and hide the one that matters (``sum(revenue)``).

Rules, in order:

1. ``tables.<name>.measures`` in ``bricksentry.yml`` is a list of columns: only
   those count. ``measures: all`` counts every numeric column (the 0.1.7 behaviour).
2. Otherwise every numeric column counts, except key columns and columns whose
   name looks like an identifier, code, date part, coordinate, rating, score,
   rate or version (``customer_id``, ``zip``, ``order_year``, ``rating``, ``lat``...).

A policy rule that names a column (``when: {column: rating, sum_change_pct_gt: 1}``)
always checks that column, whatever this module says.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_NOT_A_MEASURE = {
    # identifiers and codes
    "id", "ids", "key", "sk", "pk", "fk", "uuid", "guid", "code", "zip", "zipcode",
    "postal", "postcode", "phone", "fax", "ssn", "ein", "npi", "isbn", "sku", "hash",
    "seq", "sequence", "index", "idx", "version", "ver", "revision", "build",
    # date parts and times
    "year", "yr", "yyyy", "month", "mm", "day", "dd", "dow", "week", "wk", "quarter",
    "qtr", "hour", "hr", "minute", "second", "date", "dt", "ts", "time", "timestamp",
    "epoch", "fiscal",
    # places
    "lat", "latitude", "lon", "lng", "long", "longitude", "geo", "fips",
    # ratios and grades: their sum has no meaning
    "rating", "rank", "ranking", "score", "grade", "tier", "level", "pct", "percent",
    "percentage", "ratio", "rate", "share", "prob", "probability", "flag", "bool",
}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(column: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _CAMEL.sub("_", column).lower()) if w]


def looks_like_measure(column: str) -> bool:
    """Name heuristic only: False for ``customer_id``, ``orderYear``, ``rating``; True for ``amount``."""
    words = _words(column)
    return bool(words) and not any(w in _NOT_A_MEASURE for w in words)


def is_measure(column: str, keys: Iterable[str] = (), measures: list[str] | str | None = None) -> bool:
    """Should a change in ``sum(column)`` be reported and checked by default?"""
    if isinstance(measures, str):
        if measures.strip().lower() == "all":
            return True
        measures = [measures]
    if measures:
        return column.lower() in {m.lower() for m in measures}
    if column.lower() in {k.lower() for k in keys}:
        return False
    return looks_like_measure(column)
