"""``preview_write``: see what a write would do *before* it touches production.

1. Clone the target into the sandbox schema (Delta SHALLOW CLONE on Databricks:
   metadata only, no data copied).
2. Point the write at the clone (``{{target}}`` placeholder, or the target name
   is rewritten to the clone name).
3. Run the write against the clone and diff clone vs. the untouched target.
4. Drop the clone (unless ``keep=True``).

Guards: a single statement only, it must be INSERT / MERGE / UPDATE / DELETE /
CREATE OR REPLACE TABLE whose write target is the clone, and the production
table name must not survive the rewrite. So a preview cannot write to production.
"""

from __future__ import annotations

import re
import uuid

PLACEHOLDER = "{{target}}"

_WRITE_RE = re.compile(
    r"^\s*(?:INSERT\s+(?:INTO|OVERWRITE)(?:\s+TABLE)?|MERGE\s+INTO|UPDATE|DELETE\s+FROM|"
    r"CREATE\s+OR\s+REPLACE\s+TABLE|REPLACE\s+TABLE)\s+(?P<target>[`\"\w.\-]+)",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(r"\b(DROP|TRUNCATE|GRANT|REVOKE|ALTER|VACUUM|RESTORE)\b", re.IGNORECASE)


class PreviewGuardError(ValueError):
    pass


# What to do instead when a statement is refused. preview_write only runs data
# writes on a clone; these commands change the table itself, its permissions or
# its history, so they belong in a reviewed migration, not an agent's dry run.
SAFE_ALTERNATIVES = {
    "DROP": "To test removing all rows, preview `DELETE FROM {{target}}`. To retire a table, make it a reviewed "
            "migration and run whats_downstream first to see who still reads it.",
    "TRUNCATE": "Preview `DELETE FROM {{target}}` (optionally with a WHERE clause) to see the same effect on a clone.",
    "ALTER": "To test a schema change, preview `CREATE OR REPLACE TABLE {{target}} AS SELECT ...` with the new "
             "columns or types; the diff shows dropped columns and type changes. Apply the real ALTER in a reviewed migration.",
    "GRANT": "Permissions are not data changes, so there is nothing to diff. Change grants in Unity Catalog through a reviewed process.",
    "REVOKE": "Permissions are not data changes, so there is nothing to diff. Change grants in Unity Catalog through a reviewed process.",
    "VACUUM": "VACUUM removes old versions and limits rollback. Run it as scheduled maintenance, not as part of a change.",
    "RESTORE": "To undo a change, use rollback_plan(table) to get the exact RESTORE statement and run it with a human's approval.",
}


def _refusal(keyword: str) -> str:
    k = keyword.upper()
    hint = SAFE_ALTERNATIVES.get(k, "")
    msg = f"preview_write refuses {k}: it only runs INSERT / MERGE / UPDATE / DELETE / CREATE OR REPLACE TABLE on a clone."
    return f"{msg} Instead: {hint}" if hint else msg


def sandbox_name(sandbox_schema: str, target: str) -> str:
    short = target.split(".")[-1]
    return f"{sandbox_schema}.{short}__preview_{uuid.uuid4().hex[:8]}"


def _strip_quotes(name: str) -> str:
    return name.replace("`", "").replace('"', "").lower()


def _strip_sql_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)


def rewrite_for_preview(sql: str, target: str, clone: str, quote) -> str:
    """Return SQL that writes to ``clone`` instead of ``target``. Raises PreviewGuardError if unsafe."""
    body = sql.strip().rstrip(";").strip()
    code = _strip_sql_comments_and_strings(body)
    if ";" in code:
        raise PreviewGuardError(
            "preview_write accepts exactly one SQL statement. Preview each write separately, in the order they would run."
        )
    forbidden = _FORBIDDEN_RE.search(code)
    if forbidden:
        raise PreviewGuardError(_refusal(forbidden.group(1)))

    clone_q = ".".join(quote(p) for p in clone.split("."))
    if PLACEHOLDER in body:
        out = body.replace(PLACEHOLDER, clone_q)
    else:
        parts = [re.escape(p) for p in target.split(".")]
        pattern = r"(?<![\w.])" + r"\.".join(rf"[`\"]?{p}[`\"]?" for p in parts) + r"(?![\w])"
        out, n = re.subn(pattern, clone_q, body, flags=re.IGNORECASE)
        if n == 0:
            raise PreviewGuardError(
                f"Could not find {target} in the SQL. Write it literally or use the {PLACEHOLDER} placeholder."
            )

    m = _WRITE_RE.match(out)
    if not m:
        raise PreviewGuardError("Statement must start with INSERT, MERGE, UPDATE, DELETE or CREATE OR REPLACE TABLE")
    if _strip_quotes(m.group("target")) != _strip_quotes(clone):
        raise PreviewGuardError(f"The write target must be {target}; got {m.group('target')}")

    leftover = re.compile(r"(?<![\w.])" + re.escape(_strip_quotes(target)) + r"(?![\w])")
    if leftover.search(_strip_quotes(_strip_sql_comments_and_strings(out))):
        raise PreviewGuardError("Production table name still present after rewrite; refusing to run")
    return out
