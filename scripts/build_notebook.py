"""Build examples/databricks/bricksentry_quickstart.ipynb from the Databricks source notebook.

    python scripts/build_notebook.py

The .py file is the source of truth; edit it, then rerun this script.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "examples" / "databricks" / "bricksentry_quickstart.py"
OUT = SRC.with_suffix(".ipynb")


def cells_from_source(text: str) -> list[dict]:
    assert text.startswith("# Databricks notebook source"), "not a Databricks source notebook"
    body = text.split("\n", 1)[1]
    cells = []
    for raw in body.split("# COMMAND ----------"):
        lines = raw.strip("\n").splitlines()
        if not lines:
            continue
        if all(ln.startswith("# MAGIC") for ln in lines):
            stripped = [ln[len("# MAGIC"):].removeprefix(" ") for ln in lines]
            if stripped[0].startswith("%md"):
                md = stripped[1:]
                cells.append({"cell_type": "markdown", "metadata": {}, "source": _src(md)})
            else:
                cells.append(_code(stripped))
        else:
            cells.append(_code(lines))
    return cells


def _src(lines: list[str]) -> list[str]:
    return [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines else [])


def _code(lines: list[str]) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _src(lines)}


def build() -> dict:
    return {
        "cells": cells_from_source(SRC.read_text()),
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "application/vnd.databricks.v1+notebook": {"notebookName": "bricksentry_quickstart", "language": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n")
    print("wrote", OUT.relative_to(ROOT))
