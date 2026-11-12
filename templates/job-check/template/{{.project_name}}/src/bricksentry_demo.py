# Databricks job task for the demo: makes a known-bad change to the demo table.
# See ../README.md. Options: python -m bricksentry.job_demo --help
import sys

from bricksentry.job_demo import main

main(sys.argv[1:])
