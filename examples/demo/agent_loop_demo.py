"""Runnable demo (no Databricks needed). Same as running `bricksentry demo`:

    pip install "bricksentry[duckdb]"
    bricksentry demo                 # or: python examples/demo/agent_loop_demo.py

The demo code lives in src/bricksentry/demo.py.
"""

from bricksentry.demo import run_demo

if __name__ == "__main__":
    run_demo(report_path=None)
