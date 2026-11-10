"""Keep the Databricks quickstart notebook valid: every code cell compiles,
it calls only agent tools that exist, and the .ipynb copy is up to date."""

import ast
import json
import sys
from pathlib import Path

from bricksentry import agent_tools

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_notebook  # noqa: E402


def code_cells():
    nb = build_notebook.build()
    for c in nb["cells"]:
        if c["cell_type"] == "code":
            src = "".join(c["source"])
            if not src.lstrip().startswith("%"):
                yield src


def test_code_cells_compile():
    cells = list(code_cells())
    assert len(cells) >= 10
    for src in cells:
        ast.parse(src)


def test_uses_only_existing_agent_tools():
    used = set()
    for src in code_cells():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "agent_tools":
                used.add(node.attr)
    assert used, "notebook should use agent_tools"
    missing = {u for u in used if not hasattr(agent_tools, u)}
    assert not missing, f"notebook calls missing agent tools: {missing}"


def test_ipynb_is_up_to_date():
    on_disk = json.loads(build_notebook.OUT.read_text())
    assert on_disk == json.loads(json.dumps(build_notebook.build())), "run: python scripts/build_notebook.py"
