"""0.1.34: a webhook URL passed where a secret reference belongs is never printed."""

import pytest
from test_0114 import FakeDelta

from bricksentry import job

URL = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN123"


@pytest.mark.parametrize("ref", [URL, "xoxb-SECRETTOKEN123", "scope/key/SECRETTOKEN123", "a b/SECRETTOKEN123"])
def test_malformed_secret_reference_is_not_echoed(ref):
    problems: list[str] = []
    assert job._secret_or_problem(ref, "--slack-secret", problems) is None
    assert "SECRETTOKEN123" not in problems[0]
    assert "must name a Databricks secret as scope/key" in problems[0]
    with pytest.raises(SystemExit) as e:
        job._secret_or_problem(ref, "--teams-secret", None)
    assert "SECRETTOKEN123" not in str(e.value)


def test_valid_reference_still_named_in_errors(monkeypatch):
    def boom(ref):
        raise RuntimeError("Scope bricksentry does not exist!")

    monkeypatch.setattr(job, "read_secret", boom)
    problems: list[str] = []
    job._secret_or_problem("bricksentry/slack_webhook", "--slack-secret", problems)
    assert "--slack-secret bricksentry/slack_webhook: could not read the secret" in problems[0]


def test_job_with_pasted_url_never_prints_it():
    lines: list[str] = []
    code = job.run(["--tables", "sales.orders", "--scratch-schema", "scratch", "--slack-secret", URL],
                   backend=FakeDelta(), out=lines.append)
    text = "\n".join(lines)
    assert code == 2 and "SECRETTOKEN123" not in text and "hooks.slack.com" not in text
    assert "isn't printed here" in text
