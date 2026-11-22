"""Find and drop clones that bricksentry left behind.

Every clone is dropped when its run ends, even on Ctrl-C. Only a run that is killed
outright (the process or the cluster dies) can leave one behind. Such a clone is inert,
but it takes a name in the scratch schema, so ``bricksentry cleanup`` lists and drops them.

It only ever touches tables whose names match bricksentry's own clone names exactly:

* ``<table>__preview_<8 hex>`` and ``..._out`` (preview_write)
* ``bs_check_<table>_v<version>_<8 hex>`` (check-last-change and the job)

so your own tables in the same schema are never matched.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

CLONE_PATTERNS = (
    re.compile(r"^[A-Za-z0-9_\-]+__preview_[0-9a-f]{8}(?:_out)?$"),
    re.compile(r"^bs_check_[A-Za-z0-9_]{1,40}_v\d+_[0-9a-f]{8}$"),
)


def is_clone_name(name: str) -> bool:
    return any(p.match(name) for p in CLONE_PATTERNS)


def _age_hours(created: Any, now: datetime) -> float | None:
    if created in (None, ""):
        return None
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created / 1000 if created > 1e11 else created, timezone.utc)
    elif isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600


def find_leftovers(backend: Any, schema: str, older_than_hours: float = 6.0,
                   now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Returns (clones old enough to drop, clones skipped because they are recent or their age is
    unknown). A recent clone may belong to a run that is still going, so it is never dropped."""
    now = now or datetime.now(timezone.utc)
    drop, skip = [], []
    for t in backend.list_tables(schema):
        name = t["name"]
        if not is_clone_name(name):
            continue
        age = _age_hours(t.get("created"), now)
        if older_than_hours <= 0 or (age is not None and age >= older_than_hours):
            drop.append(f"{schema}.{name}")
        else:
            skip.append(f"{schema}.{name}")
    return sorted(drop), sorted(skip)


def drop_leftovers(backend: Any, tables: list[str]) -> list[tuple[str, str | None]]:
    """Drop each table; returns [(table, error or None)]."""
    done = []
    for t in tables:
        if not is_clone_name(t.split(".")[-1]):  # never trust a caller's list
            done.append((t, "not a bricksentry clone name; skipped"))
            continue
        try:
            backend.drop(t)
            done.append((t, None))
        except Exception as e:
            done.append((t, (str(e).strip().splitlines() or [type(e).__name__])[0][:200]))
    return done
