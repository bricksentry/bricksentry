# Databricks job task: check what changed in the watched tables. See ../README.md.
# All options: python -m bricksentry.job --help
import sys

from bricksentry.job import main

main(sys.argv[1:])
