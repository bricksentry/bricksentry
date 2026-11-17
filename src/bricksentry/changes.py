"""Work out which tables a pull request touches.

Each table in ``bricksentry.yml`` lists the repo paths that produce it
(notebooks, SQL files, Databricks Asset Bundle resource YAML...). A changed
file matching a table's globs marks that table as changed.
"""

from __future__ import annotations

import fnmatch
import subprocess

from bricksentry.config import Config


class GitError(RuntimeError):
    pass


def changed_files(base_ref: str = "origin/main", cwd: str | None = None) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        ).stdout
    except FileNotFoundError:
        raise GitError("--from-git needs git, which isn't installed here. List the tables instead.") from None
    except subprocess.CalledProcessError as e:
        why = (e.stderr or "").strip().splitlines()
        raise GitError(
            f"git diff {base_ref}...HEAD failed: {why[0] if why else 'exit ' + str(e.returncode)}. Run it inside the "
            f"repository, and fetch {base_ref} first (in CI: actions/checkout with fetch-depth: 0)."
        ) from None
    return [line.strip() for line in out.splitlines() if line.strip()]


def tables_for_files(cfg: Config, files: list[str]) -> list[str]:
    hits: list[str] = []
    for name, spec in cfg.tables.items():
        for pat in spec.paths:
            if any(fnmatch.fnmatch(f, pat) for f in files):
                hits.append(name)
                break
    return hits
