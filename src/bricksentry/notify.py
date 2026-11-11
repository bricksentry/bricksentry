"""Send a short verdict alert to Slack or Microsoft Teams (stdlib only).

Configure in ``bricksentry.yml``::

    notify:
      on: needs_human                         # alert at this verdict or worse (default)
      slack_webhook: env:SLACK_WEBHOOK_URL    # read the URL from an environment variable
      teams_webhook: env:TEAMS_WEBHOOK_URL
      mentions:                               # owner -> Slack member/group id, to @-mention reviewers
        finance-analytics: S0123ABCD          # a user group (starts with S)
        jane@acme.com: U0456EFGH              # a person (starts with U or W)
      run_url: env:BRICKSENTRY_RUN_URL        # optional link shown on the alert

or just set ``BRICKSENTRY_SLACK_WEBHOOK`` / ``BRICKSENTRY_TEAMS_WEBHOOK``
(and optionally ``BRICKSENTRY_NOTIFY_ON`` and ``BRICKSENTRY_RUN_URL``).
Webhook URLs are secrets: keep them in environment variables or CI secrets,
not in the YAML file itself.

Slack uses an incoming webhook. Teams uses a Workflows ("Post to a channel when a
webhook request is received") URL, which accepts an Adaptive Card.

The alert links back to where the check ran: the pull request or Actions run in
GitHub Actions, the job run on Databricks, or ``run_url`` if you set one.

Alerts never fail a run: a webhook error is reported, retried once if Slack or
Teams is busy, and the verdict stands. Error messages never include the webhook URL.

Try your setup with ``bricksentry test-alert``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from bricksentry.models import Assessment, RuleHit, TableAssessment, Verdict

_EMOJI = {"pass": ":large_green_circle:", "warn": ":large_yellow_circle:",
          "needs_human": ":large_orange_circle:", "block": ":red_circle:"}
_TITLE = {"pass": "Safe to ship", "warn": "Safe to ship, with warnings",
          "needs_human": "Needs human review", "block": "Blocked"}
_MAX_REASON = 300
_SLACK_ID = re.compile(r"^[UWS][A-Z0-9]{6,}$")


@dataclass
class NotifySettings:
    on: Verdict = Verdict.NEEDS_HUMAN
    slack_webhook: str | None = None
    teams_webhook: str | None = None
    mentions: dict[str, str] = field(default_factory=dict)
    run_url: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.slack_webhook or self.teams_webhook)

    @classmethod
    def from_config(cls, data: dict[str, Any] | None) -> NotifySettings:
        data = data or {}
        on = str(data.get("on") or os.environ.get("BRICKSENTRY_NOTIFY_ON") or "needs_human").strip().lower()
        try:
            verdict = Verdict(on)
        except ValueError:
            raise ValueError(
                f"notify.on must be one of pass, warn, needs_human, block (got {on!r})"
            ) from None
        mentions = data.get("mentions") or {}
        if not isinstance(mentions, dict):
            raise ValueError("notify.mentions must map an owner to a Slack id, e.g. {finance-analytics: S0123ABCD}")
        return cls(
            on=verdict,
            slack_webhook=_resolve(data.get("slack_webhook")) or os.environ.get("BRICKSENTRY_SLACK_WEBHOOK") or None,
            teams_webhook=_resolve(data.get("teams_webhook")) or os.environ.get("BRICKSENTRY_TEAMS_WEBHOOK") or None,
            mentions={str(k).strip().lower(): str(v).strip() for k, v in mentions.items() if v},
            run_url=_resolve(data.get("run_url")) or os.environ.get("BRICKSENTRY_RUN_URL") or None,
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


# --- where the check ran ------------------------------------------------------
def _github_link(pr: str | None) -> tuple[str, str] | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    if pr:
        return f"{server}/{repo}/pull/{pr}", "Open pull request"
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        return f"{server}/{repo}/actions/runs/{run}", "Open GitHub run"
    return None


def _databricks_context() -> dict[str, Any]:  # pragma: no cover - only works on Databricks
    from databricks.sdk.runtime import dbutils

    ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    return json.loads(ctx.safeToJson()).get("attributes") or {}


def _databricks_link() -> tuple[str, str] | None:
    """Link to the Databricks job run this code is part of, when it runs in a job."""
    if not os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        return None
    try:
        attrs = _databricks_context()
    except Exception:
        return None
    job = attrs.get("jobId")
    run = attrs.get("multitaskParentRunId") or attrs.get("rootRunId") or attrs.get("currentRunId")
    host = attrs.get("browserHostName") or (os.environ.get("DATABRICKS_HOST") or "")
    host = re.sub(r"^https?://", "", str(host)).strip("/")
    if not (job and run and host):
        return None
    org = attrs.get("orgId")
    return f"https://{host}/jobs/{job}/runs/{run}" + (f"?o={org}" if org else ""), "Open Databricks run"


def run_link(pr: str | None = None, run_url: str | None = None) -> tuple[str, str] | None:
    """(url, label) for the place this check ran, or None."""
    if run_url and run_url.startswith(("https://", "http://")):
        return run_url, "Open run"
    return _github_link(pr) or _databricks_link()


# --- message ------------------------------------------------------------------
def _clip(s: str, n: int = _MAX_REASON) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _reasons(a: Assessment, max_reasons: int) -> list[RuleHit]:
    return sorted(a.hits, key=lambda h: -h.verdict.rank)[:max_reasons]


def _title(a: Assessment) -> str:
    v = a.verdict.value
    return f"bricksentry: {_TITLE[v]} (risk {a.verdict.risk_tier})"


def _slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_mention(owner: str, mentions: dict[str, str]) -> str:
    sid = mentions.get(owner.lower())
    if sid and _SLACK_ID.match(sid):
        return f"<!subteam^{sid}>" if sid.startswith("S") else f"<@{sid}>"
    if sid in ("here", "channel"):
        return f"<!{sid}>"
    return _slack_escape(owner)


def _version() -> str:
    try:
        from bricksentry import __version__

        return __version__
    except Exception:  # pragma: no cover
        return ""


def slack_payload(
    a: Assessment,
    pr: str | None = None,
    mentions: dict[str, str] | None = None,
    run_url: str | None = None,
    max_reasons: int = 5,
) -> dict[str, Any]:
    mentions = mentions or {}
    title = _title(a)
    lines = [f"*Tables:* {_slack_escape(', '.join(t.table for t in a.tables))}"]
    for h in _reasons(a, max_reasons):
        lines.append(f"`{h.verdict.value}` *{_slack_escape(h.rule)}* on {_slack_escape(h.table)}: {_slack_escape(_clip(h.reason))}")
    if len(a.hits) > max_reasons:
        lines.append(f"_…and {len(a.hits) - max_reasons} more_")
    if a.notify:
        lines.append("*Review requested from:* " + ", ".join(_slack_mention(o, mentions) for o in a.notify))
    link = run_link(pr, run_url)
    if link:
        lines.append(f"<{link[0]}|{link[1]}>")
    body = "\n".join(lines)
    if len(body) > 2900:
        body = body[:2899] + "…"
    footer = f"bricksentry {_version()} · assessment `{a.id}`".replace("  ", " ")
    return {
        "text": title,  # notification fallback
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{_EMOJI[a.verdict.value]} *{title}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def _plain(text: str) -> dict[str, Any]:
    # RichTextBlock + TextRun shows text as-is: no markdown, so `my_table_name` stays intact.
    return {"type": "RichTextBlock", "spacing": "Small", "inlines": [{"type": "TextRun", "text": text}]}


def teams_payload(
    a: Assessment,
    pr: str | None = None,
    run_url: str | None = None,
    max_reasons: int = 5,
) -> dict[str, Any]:
    color = {"pass": "Good", "warn": "Warning", "needs_human": "Warning", "block": "Attention"}[a.verdict.value]
    body: list[dict[str, Any]] = [
        {"type": "RichTextBlock", "inlines": [
            {"type": "TextRun", "text": _title(a), "weight": "Bolder", "size": "Medium", "color": color}
        ]},
        _plain(f"Tables: {', '.join(t.table for t in a.tables)}"),
    ]
    for h in _reasons(a, max_reasons):
        body.append(_plain(f"[{h.verdict.value}] {h.rule} on {h.table}: {_clip(h.reason)}"))
    if len(a.hits) > max_reasons:
        body.append(_plain(f"…and {len(a.hits) - max_reasons} more"))
    if a.notify:
        body.append(_plain(f"Review requested from: {', '.join(a.notify)}"))
    body.append({"type": "RichTextBlock", "spacing": "Medium", "inlines": [
        {"type": "TextRun", "text": f"bricksentry {_version()} · assessment {a.id}", "isSubtle": True, "size": "Small"}
    ]})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    link = run_link(pr, run_url)
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": link[1], "url": link[0]}]
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }


# --- delivery -----------------------------------------------------------------
class _DeliveryError(Exception):
    pass


def _post(url: str, payload: dict[str, Any], timeout: int = 15) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"bricksentry/{_version()}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _describe(e: Exception, url: str) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = e.read().decode(errors="replace").strip()[:120]
        except Exception:
            detail = ""
        hint = {
            400: "the service rejected the message",
            403: "the webhook is not allowed to post here",
            404: "webhook not found: it was revoked or the app was removed",
            410: "webhook was revoked",
            429: "rate limited",
        }.get(e.code, "")
        msg = f"HTTP {e.code}" + (f" {detail}" if detail else "") + (f" ({hint})" if hint else "")
    elif isinstance(e, urllib.error.URLError):
        msg = f"could not connect: {e.reason}"
    else:
        msg = f"{type(e).__name__}: {e}"
    # Never echo the secret webhook URL (or any URL) back into logs.
    msg = msg.replace(url, "<webhook>")
    return re.sub(r"https?://\S+", "<url>", msg)


def _retryable(e: Exception) -> float | None:
    """Seconds to wait before one retry, or None if retrying won't help."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 429 or e.code >= 500:
            try:
                return min(max(float(e.headers.get("Retry-After") or 1), 0.5), 5.0)
            except (TypeError, ValueError):
                return 1.0
        return None
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return 1.0
    return None


def _deliver(url: str, payload: dict[str, Any], attempts: int = 2) -> None:
    for i in range(attempts):
        try:
            _post(url, payload)
            return
        except Exception as e:
            wait = _retryable(e)
            if wait is None or i == attempts - 1:
                raise _DeliveryError(_describe(e, url)) from None
            time.sleep(wait)


def send(a: Assessment, settings: NotifySettings, pr: str | None = None, force: bool = False) -> list[str]:
    """Send alerts for ``a``. Returns one status line per channel; never raises."""
    out: list[str] = []
    if not settings.enabled:
        return out
    if not force and a.verdict.rank < settings.on.rank:
        return [f"no alert: verdict {a.verdict.value} is below notify.on={settings.on.value}"]
    channels = (
        ("slack", settings.slack_webhook,
         lambda: slack_payload(a, pr, mentions=settings.mentions, run_url=settings.run_url)),
        ("teams", settings.teams_webhook, lambda: teams_payload(a, pr, run_url=settings.run_url)),
    )
    for name, url, build in channels:
        if not url:
            continue
        if not url.startswith("https://"):
            out.append(f"{name}: skipped, webhook URL must start with https://")
            continue
        try:
            _deliver(url, build())
            out.append(f"{name}: sent")
        except _DeliveryError as e:
            out.append(f"{name}: failed ({e})")
        except Exception as e:  # an alert must never change the verdict or crash CI
            out.append(f"{name}: failed ({_describe(e, url)})")
    return out


def sample_assessment(verdict: Verdict = Verdict.NEEDS_HUMAN, owner: str | None = None) -> Assessment:
    """A clearly labelled fake assessment for ``bricksentry test-alert``: no table is read."""
    table = "example.sales.orders"
    hits = [
        RuleHit("test-alert", verdict, table,
                "This is a test alert from `bricksentry test-alert`. No data was read or changed."),
        RuleHit("row-loss", Verdict.NEEDS_HUMAN if verdict.rank >= Verdict.NEEDS_HUMAN.rank else verdict, table,
                "example: removed 2.40% of rows (> 1%)"),
        RuleHit("numeric-total-moved", Verdict.WARN, table, "example: sum(amount) -3.10%"),
    ]
    ta = TableAssessment(table=table, diff=None, downstream=[], owner=owner, tags={}, rollback=None,
                         verdict=verdict, hits=hits, summary="test alert")
    return Assessment(id="test" + os.urandom(4).hex(), tables=[ta], verdict=verdict,
                      notify=[owner] if owner else [])
