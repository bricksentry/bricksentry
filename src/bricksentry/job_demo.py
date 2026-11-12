"""Make a known-bad change to a demo table, so the first bricksentry check you see is a block.

Used by the ``bricksentry_demo`` job in the bundle template (``templates/job-check``)::

    python -m bricksentry.job_demo --table main.bricksentry_scratch.bricksentry_demo_orders --warehouse-id abc123

It (re)creates a 20,000-row orders table, then runs a MERGE whose ON condition compares
region case-sensitively ('apac' vs 'APAC'). Nothing matches, so 25 orders that should have
been updated are inserted again as duplicates. The next task checks that change.
Only the table named by ``--table`` is written to.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Any

SEED_SQL = """CREATE OR REPLACE TABLE {t} AS
SELECT id AS order_id,
       element_at(array('APAC', 'EMEA', 'AMER'), CAST(1 + id % 3 AS INT)) AS region,
       CAST(round(20 + (id % 97) * 3.1, 2) AS DOUBLE) AS amount,
       CASE WHEN id % 10 = 0 THEN 'refunded' ELSE 'paid' END AS status
FROM range(1, 20001)"""

# Late price corrections for 25 orders. The bug: the source has region in lower case and
# the ON condition compares it case-sensitively, so no row matches and all 25 are inserted.
BAD_MERGE_SQL = """MERGE INTO {t} AS t
USING (
  SELECT order_id, lower(region) AS region, round(amount * 1.05, 2) AS amount, status
  FROM {t} WHERE order_id % 800 = 0
) AS s
ON t.order_id = s.order_id AND t.region = s.region
WHEN MATCHED THEN UPDATE SET t.amount = s.amount
WHEN NOT MATCHED THEN INSERT (order_id, region, amount, status)
  VALUES (s.order_id, upper(s.region), s.amount, s.status)"""

_NAME = re.compile(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+")


def run(argv: list[str] | None = None, backend: Any = None, out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m bricksentry.job_demo", description=__doc__.split("\n\n")[0])
    p.add_argument("--table", required=True, help="catalog.schema.table for the demo (created or replaced)")
    p.add_argument("--warehouse-id", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"))
    args = p.parse_args(argv)
    if not _NAME.fullmatch(args.table):
        raise SystemExit(f"--table must be catalog.schema.table, got {args.table!r}")
    if not args.table.split(".")[-1].startswith("bricksentry_demo"):
        raise SystemExit("For safety the demo only writes to a table whose name starts with bricksentry_demo.")
    if backend is None:  # pragma: no cover - needs Databricks
        from bricksentry.backends.databricks_backend import DatabricksBackend

        backend = DatabricksBackend(warehouse_id=args.warehouse_id)
    from bricksentry.history import _ensure_scratch

    t = backend.qualified(args.table)
    try:
        _ensure_scratch(backend, ".".join(args.table.split(".")[:2]))
    except RuntimeError as e:
        raise SystemExit(str(e)) from None
    backend.execute(SEED_SQL.format(t=t))
    out(f"Created {args.table}: 20,000 orders, one row per order_id.")
    backend.execute(BAD_MERGE_SQL.format(t=t))
    out("Ran a MERGE with a bug: its ON condition compares region case-sensitively ('apac' vs 'APAC'),")
    out("so 25 orders it should have updated were inserted again as duplicates.")
    out("The next task checks this change. Expect BLOCK (duplicate-keys), and the RESTORE that undoes it.")
    return 0


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin wrapper
    run(argv)  # no sys.exit on success: Databricks marks SystemExit(0) as a failed task


if __name__ == "__main__":  # pragma: no cover
    main()
