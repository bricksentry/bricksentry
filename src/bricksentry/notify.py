"""Send a short verdict alert to Slack or Microsoft Teams (stdlib only).

Configure in ``bricksentry.yml``::

    notify:
      on: needs_human                         # alert at this verdict or worse (default)
      slack_webhook: env:SLACK_WEBHOOK_URL    # read the URL from an environment variable
      teams_webhook: env:TEAMS_WEBHOOK_URL

or just set ``BRICKSENTRY_SLACK_WEBHOOK`` / ``BRICKSENTRY_TEAMS_WEBHOOK``
(and optionally ``BRICKSENTRY_NOTIFY_ON``).
Webhook URLs are secrets: keep them in environment variables or CI secrets,
not in the YAML file itself.

Slack uses an incoming webhook. Teams uses a Workflows ("Post to a channel when a
webhook request is received") URL, which accepts an Adaptive Card.

Alerts never fail a run: a webhook error is reported and the verdict stands.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from bricksentry.models import Assessment, Verdict

_EMOJI = {"pass": ":large_green_circle:", "warn": ":large_yellow_circle:",
          "needs_human": ":large_orange_circle:", "block": ":red_circle:"}
_TITLE = {"pass": "Safe to ship", "warn": "Safe to ship, with warnings",
          "needs_human": "Needs human review", "block": "Blocked"}


@dataclass
class NotifySettings:
    on: Verdict = Verdict.NEEDS_HUMAN
    slack_webhook: str | None = None
    teams_webhook: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.slack_webhook or self.teams_webhook)

    @classmethod
    def from_config(cls, data: dict[str, Any] | None) -> NotifySettings:
        data = data or {}
        return cls(
            on=Verdict(str(data.get("on") or os.environ.get("BRICKSENTRY_NOTIFY_ON") or "needs_human")),
            slack_webhook=_resolve(data.get("slack_webhook")) or os.environ.get("BRICKSENTRY_SLACK_WEBHOOK") or None,
            teams_webhook=_resolve(data.get("teams_webhook")) or os.environ.get("BRICKSENTRY_TEAMS_WEBHOOK") or None,
        )


def _resolve(value: Any) -> str | None:
    """``env:NAME`` or ``${NAME}`` reads an environment variable; anything else is used as given."""
    if not value:
        return None
    v = str(value).strip()
    if v.startswith("env:"):
        return os.environ.get(v[4:].strip()) or None
    if v.startswith("${") and v.endswith("}"):
        return os.environ.get(v[2:-1].strip()) or None
    return v


def _pr_link(pr: str | None) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    if repo and pr:
        return f"{server}/{repo}/pull/{pr}"
    run = os.environ.get("GITHUB_RUN_ID")
    if repo and run:
        return f"{server}/{repo}/actions/runs/{run}"
    return None


def _lines(a: Assessment, pr: str | None, max_reasons: int = 5) -> tuple[str, list[str], str | None]:
    v = a.verdict.value
    title = f"bricksentry: {_TITLE[v]} (risk {a.verdict.risk_tier})"
    lines = [f"Tables: {', '.join(t.table for t in a.tables)}"]
    hits = sorted(a.hits, key=lambda h: -h.verdict.rank)
    for h in hits[:max_reasons]:
        lines.append(f"[{h.verdict.value}] {h.rule} on {h.table}: {h.reason}")
    if len(hits) > max_reasons:
        lines.append(f"...and {len(hits) - max_reasons} more")
    if a.notify:
        lines.append(f"Review requested from: {', '.join(a.notify)}")
    lines.append(f"Assessment: {a.id}")
    return title, lines, _pr_link(pr)


def slack_payload(a: Assessment, pr: str | None = None) -> dict[str, Any]:
    title, lines, link = _lines(a, pr)
    head = f"{_EMOJI[a.verdict.value]} *{title}*"
    body = "\n".join(lines) + (f"\n<{link}|Open in GitHub>" if link else "")
    return {
        "text": f"{title}",  # notification fallback
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": head}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
        ],
    }


def teams_payload(a: Assessment, pr: str | None = None) -> dict[str, Any]:
    title, lines, link = _lines(a, pr)
    color = {"pass": "Good", "warn": "Warning", "needs_human": "Warning", "block": "Attention"}[a.verdict.value]
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": color, "wrap": True},
    ] + [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "Small"} for line in lines]
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in GitHub", "url": link}]
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


def _post(url: str, payload: dict[str, Any], timeout: int = 15) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def send(a: Assessment, settings: NotifySettings, pr: str | None = None, force: bool = False) -> list[str]:
    """Send alerts for ``a``. Returns one status line per channel; never raises."""
    out: list[str] = []
    if not settings.enabled:
        return out
    if not force and a.verdict.rank < settings.on.rank:
        return [f"no alert: verdict {a.verdict.value} is below notify.on={settings.on.value}"]
    for name, url, build in (
        ("slack", settings.slack_webhook, slack_payload),
        ("teams", settings.teams_webhook, teams_payload),
    ):
        if not url:
            continue
        if not url.startswith("https://"):
            out.append(f"{name}: skipped, webhook URL must start with https://")
            continue
        try:
            _post(url, build(a, pr))
            out.append(f"{name}: sent")
        except Exception as e:  # an alert must never change the verdict or crash CI
            out.append(f"{name}: failed ({type(e).__name__}: {e})")
    return out
