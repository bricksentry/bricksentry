"""bricksentry: change-safety for Databricks pipelines and the AI agents that edit them.

One engine, three faces:

* CLI / GitHub Action for data engineers and CI
* Python API for notebooks, jobs and tests
* MCP server for AI coding agents

Every face answers the same question: *is this change safe, what will it break,
and how do I undo it?*
"""

from bricksentry.assess import Sentry
from bricksentry.config import Config, TableSpec
from bricksentry.models import Assessment, ImpactNode, TableDiff, Verdict

__all__ = [
    "Sentry",
    "Config",
    "TableSpec",
    "Assessment",
    "ImpactNode",
    "TableDiff",
    "Verdict",
]
__version__ = "0.1.0"
