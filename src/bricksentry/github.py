"""Post or update a single bricksentry comment on a GitHub pull request (stdlib only)."""

from __future__ import annotations

import json
import os
import urllib.request

from bricksentry.report import MARKER


def _req(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def upsert_pr_comment(
    markdown: str,
    repo: str | None = None,
    pr: int | str | None = None,
    token: str | None = None,
    api: str | None = None,
) -> str:
    repo = repo or os.environ["GITHUB_REPOSITORY"]
    token = token or os.environ["GITHUB_TOKEN"]
    api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if pr is None:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path and os.path.exists(event_path):
            with open(event_path) as f:
                pr = json.load(f).get("pull_request", {}).get("number")
    if not pr:
        raise ValueError("Pull request number not found; pass --pr")
    base = f"{api}/repos/{repo}/issues/{pr}/comments"
    page = 1
    while True:
        comments = _req("GET", f"{base}?per_page=100&page={page}", token) or []
        for c in comments:
            if MARKER in (c.get("body") or ""):
                _req("PATCH", f"{api}/repos/{repo}/issues/comments/{c['id']}", token, {"body": markdown})
                return c.get("html_url", "")
        if len(comments) < 100:
            break
        page += 1
    return (_req("POST", base, token, {"body": markdown}) or {}).get("html_url", "")
