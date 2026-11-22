"""Tables with Unity Catalog column masks or row filters.

Databricks refuses both shallow clones and time travel (``VERSION AS OF``) on these tables
(``COLUMN_MASKS_FEATURE_NOT_SUPPORTED`` / ``ROW_LEVEL_SECURITY_FEATURE_NOT_SUPPORTED``).
So bricksentry can't preview a write on them (no clone) or compare two versions of them
(no time travel), and it says so up front, in one clear message, instead of failing in the
middle. It never copies governed data into the scratch schema as a workaround: the scratch
schema may be readable by more people than the table, which is what masks and filters
exist to prevent. And it never suggests removing the mask or filter.
"""

from __future__ import annotations

from typing import Any

_CODES = ("COLUMN_MASKS_FEATURE_NOT_SUPPORTED", "ROW_LEVEL_SECURITY_FEATURE_NOT_SUPPORTED")


def describe(db: Any, table: str) -> str | None:
    """"a column mask (on amount)", "a row filter", both, or None. None also when the
    protections can't be read; the engine's own error is then recognised by ``from_error``."""
    try:
        p = (db.protections([table]) or {}).get(table) or {}
    except Exception:
        return None
    parts = []
    if p.get("masked"):
        cols = ", ".join(sorted(p["masked"]))
        parts.append(f"a column mask (on {cols})")
    if p.get("row_filter"):
        parts.append("a row filter")
    return " and ".join(parts) or None


def from_error(e: BaseException) -> str | None:
    text = str(e)
    if _CODES[0] in text and _CODES[1] in text:
        return "a column mask and a row filter"
    if _CODES[0] in text:
        return "a column mask"
    if _CODES[1] in text:
        return "a row filter"
    return None


def check_message(table: str, what: str) -> str:
    return (f"{table} has {what}. Databricks doesn't allow reading earlier versions of tables with column "
            "masks or row filters (time travel and clones are refused), so check-last-change can't compare "
            "versions of it. Check changes before they reach it instead: preview or assess the write on the "
            "table that feeds it, or on a dev copy without the mask or filter.")


def preview_message(table: str, what: str) -> str:
    return (f"{table} has {what}. Databricks doesn't allow shallow clones of tables with column masks or row "
            "filters, and bricksentry won't copy governed data into the sandbox, so this write can't be "
            "previewed here. Preview it on a dev copy of the table without the mask or filter, or on the table "
            "that feeds it.")
