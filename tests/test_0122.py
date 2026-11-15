"""0.1.22: outbound URLs (webhooks, GitHub API) must be https, public, and are never redirected."""

from __future__ import annotations

import http.server
import socket
import threading
import urllib.error
import urllib.request

import pytest

from bricksentry import github, netguard, notify
from bricksentry.models import Verdict


@pytest.mark.parametrize("url", [
    "http://hooks.slack.com/services/x",          # not https
    "https://localhost/hook",
    "https://127.0.0.1/hook",
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
    "https://10.0.0.5/hook",
    "https://192.168.1.10/hook",
    "https://[::1]/hook",
    "https://[::ffff:127.0.0.1]/hook",
    "https://0.0.0.0/hook",
    "https:///nohost",
])
def test_unsafe_urls_are_refused(url):
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url(url)


def test_public_addresses_pass():
    netguard.check_url("https://8.8.8.8/x")
    netguard.check_url("https://[2001:4860:4860::8888]/x")


def test_hostname_resolving_to_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", ("10.1.2.3", 443))])
    with pytest.raises(netguard.UnsafeURLError, match="non-public"):
        netguard.check_url("https://hooks.internal.example/x")


def test_unresolvable_host_is_left_to_fail_normally(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    netguard.check_url("https://does-not-exist.example/x")


def test_private_targets_can_be_allowed(monkeypatch):
    monkeypatch.setenv(netguard.ALLOW_PRIVATE_ENV, "1")
    netguard.check_url("https://10.0.0.5/api/v3")
    with pytest.raises(netguard.UnsafeURLError):  # https is still required
        netguard.check_url("http://10.0.0.5/api/v3")


def test_redirects_are_not_followed():
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            netguard._OPENER.open(req, timeout=5)
        assert e.value.code == 302
    finally:
        srv.shutdown()


def test_webhook_to_private_address_is_skipped_not_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, payload, timeout=15: sent.append(url))
    a = notify.sample_assessment(Verdict.BLOCK)
    settings = notify.NotifySettings(slack_webhook="https://169.254.169.254/hook")
    out = notify.send(a, settings, force=True)
    assert sent == []
    assert out == ["slack: skipped, webhook URL resolves to a non-public address; "
                   "set BRICKSENTRY_ALLOW_PRIVATE_URLS=1 if that is intended"]


def test_github_api_must_be_public_https(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    with pytest.raises(netguard.UnsafeURLError):
        github.upsert_pr_comment("x", pr=1, api="https://127.0.0.1:9/api")
    with pytest.raises(netguard.UnsafeURLError):
        github.upsert_pr_comment("x", pr=1, api="http://api.github.com")
