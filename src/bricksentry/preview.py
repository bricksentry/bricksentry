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


def _scan(sql: str, backslash: bool = True, idents: bool = False) -> list[tuple[str, int, int]]:
    """Split ``sql`` into ("code" | "string" | "comment", start, end) pieces.

    '...' is a string literal. `...` and "..." are skipped over as single tokens (so a
    ``--`` or ``;`` inside them is never mistaken for a comment or a statement end) but count
    as code: they can quote the table name, and keeping their text visible to the guards can
    only cause a refusal, never hide anything.

    Engines differ on backslashes in '...' (Databricks treats them as escapes, DuckDB and
    ANSI SQL don't), so the guards look at the SQL both ways; see ``_masked``. An unclosed
    '...' or /* would hide the rest of the statement, so it is refused.
    """
    pieces: list[tuple[str, int, int]] = []
    i, start, n = 0, 0, len(sql)

    def close(kind: str, a: int, b: int) -> None:
        if b > a:
            if pieces and pieces[-1][0] == kind and pieces[-1][2] == a:
                pieces[-1] = (kind, pieces[-1][1], b)
            else:
                pieces.append((kind, a, b))

    def skip_quoted(j: int, q: str) -> int:
        j += 1
        while j < n:
            if sql[j] == "\\" and q != "`" and backslash:
                j += 2
                continue
            if sql[j] == q:
                if sql.startswith(q + q, j):
                    j += 2
                    continue
                return j + 1
            j += 1
        return n

    while i < n:
        c = sql[i]
        if c in "`\"":
            j = skip_quoted(i, c)
            if idents:  # report quoted names as their own pieces (for the keyword check)
                if j >= n and not (j - i >= 2 and sql[j - 1] == c):
                    raise PreviewGuardError(f"The SQL has a quote ({c}) that is never closed.")
                close("code", start, i)
                close("ident", i, j)
                start = j
            i = j
        elif c == "'":
            close("code", start, i)
            j = skip_quoted(i, "'")
            if j >= n and not (j - i >= 2 and sql[j - 1] == "'"):
                raise PreviewGuardError("The SQL has a quote (') that is never closed.")
            close("string", i, j)
            i = start = j
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            close("code", start, i)
            if sql[i + 1] == "-":
                j = sql.find("\n", i)
                j = n if j < 0 else j
            else:
                j = sql.find("*/", i + 2)
                if j < 0:
                    raise PreviewGuardError("The SQL has a /* comment that is never closed.")
                j += 2
            close("comment", i, j)
            i = start = j
        else:
            i += 1
    close("code", start, n)
    return pieces


_BLANK = {"comment": " ", "string": "''", "ident": "`_`"}


def _strip_sql_comments_and_strings(sql: str, backslash: bool = True, idents: bool = False) -> str:
    """The SQL with comments blanked and '...' literals emptied, for the guards to inspect.
    With ``idents``, quoted names (`drop`, "drop") are blanked too."""
    return "".join(sql[a:b] if k == "code" else _BLANK[k] for k, a, b in _scan(sql, backslash, idents))


def _masked(sql: str, idents: bool = False) -> list[str]:
    """The SQL as each engine family would read it; every guard must pass on all of them.
    ``idents`` blanks quoted names: right for keyword and ``;`` checks (a column named
    `drop` is a name, not a statement), wrong for finding the production table's name."""
    return [_strip_sql_comments_and_strings(sql, True, idents), _strip_sql_comments_and_strings(sql, False, idents)]


_LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.S)


def _sub_in_code(pattern: re.Pattern[str], repl: str, sql: str) -> tuple[str, int]:
    """Like ``pattern.subn`` but leaves comments and string literals alone, so a table
    name or ``{{target}}`` written as a value is never turned into the clone's name."""
    out, last, total = [], 0, 0
    for kind, a, b in _scan(sql):
        if kind != "code":
            continue
        out.append(sql[last:a])
        piece, k = pattern.subn(lambda _m: repl, sql[a:b])
        out.append(piece)
        total += k
        last = b
    out.append(sql[last:])
    return "".join(out), total


def statement_head(sql: str) -> str:
    """``sql`` without the comments and whitespace in front of the statement."""
    return sql[_LEADING_COMMENTS.match(sql).end():]


def rewrite_for_preview(sql: str, target: str, clone: str, quote) -> str:
    """Return SQL that writes to ``clone`` instead of ``target``. Raises PreviewGuardError if unsafe."""
    body = sql.strip().rstrip(";").strip()
    for code in _masked(body, idents=True):
        if ";" in code:
            raise PreviewGuardError(
                "preview_write accepts exactly one SQL statement. Preview each write separately, in the order they "
                "would run."
            )
        forbidden = _FORBIDDEN_RE.search(code)
        if forbidden:
            raise PreviewGuardError(_refusal(forbidden.group(1)))

    clone_q = ".".join(quote(p) for p in clone.split("."))
    if PLACEHOLDER in body:
        out, n = _sub_in_code(re.compile(re.escape(PLACEHOLDER)), clone_q, body)
        if n == 0:
            raise PreviewGuardError(
                f"{PLACEHOLDER} only appears inside a quoted string or a comment. Put it where the table name goes, "
                "for example UPDATE {{target}} SET ..."
            )
    else:
        parts = [re.escape(p) for p in target.split(".")]
        pattern = r"(?<![\w.])" + r"\.".join(rf"[`\"]?{p}[`\"]?" for p in parts) + r"(?![\w])"
        out, n = _sub_in_code(re.compile(pattern, re.IGNORECASE), clone_q, body)
        if n == 0:
            raise PreviewGuardError(
                f"Could not find {target} in the SQL. Write it literally (catalog.schema.table) or use the "
                f"{PLACEHOLDER} placeholder."
            )

    m = _WRITE_RE.match(statement_head(out))
    if not m:
        raise PreviewGuardError("Statement must start with INSERT, MERGE, UPDATE, DELETE or CREATE OR REPLACE TABLE")
    if _strip_quotes(m.group("target")) != _strip_quotes(clone):
        raise PreviewGuardError(f"The write target must be {target}; got {m.group('target')}")

    leftover = re.compile(r"(?<![\w.])" + re.escape(_strip_quotes(target)) + r"(?![\w])")
    if any(leftover.search(_strip_quotes(code)) for code in _masked(out)):
        raise PreviewGuardError("Production table name still present after rewrite; refusing to run")
    return out
