# Contributing to bricksentry

Thanks for helping. Bug reports from real pipelines are the most useful contribution right now: what bricksentry caught, what it missed, and where it got in the way.

## Report a bug or ask for a feature

Open an issue using one of the templates. For bugs, include the command you ran, your `bricksentry.yml` (remove anything private), the output, and what you expected.

Security problems: please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Develop locally

```bash
git clone https://github.com/bricksentry/bricksentry
cd bricksentry
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python examples/demo/agent_loop_demo.py
```

The tests run against DuckDB, so you don't need a Databricks workspace. The Databricks backend is tested against a mocked SDK client; if your change touches it, please say in the pull request whether you also tried it on a real workspace.

## Pull requests

- Keep each pull request to one change, with a test that fails before it and passes after.
- Verdicts must stay deterministic: policy rules decide them, never an LLM.
- `preview_write` must never be able to write to the production table. Changes to `preview.py` need tests for the guard.
- Add a line under "Unreleased" in `CHANGELOG.md`.

## Releases

Maintainers bump `version` in `pyproject.toml`, update `CHANGELOG.md`, and push a `vX.Y.Z` tag. The release workflow tests, builds and publishes to PyPI through Trusted Publishing.
