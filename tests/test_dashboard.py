"""Tests for the Grafana dashboard JSON and verify_dashboard.py.

Issue #413: source-control the agentpy-fleet dashboard and verify panels.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = REPO_ROOT / "dashboards" / "agentpy-fleet.json"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_dashboard.py"

BASE_METRICS = (
    "agentpy_up",
    "agentpy_cycles_total",
    "agentpy_cycle_duration_seconds",
    "agentpy_tokens_total",
    "agentpy_errors_total",
)

VERBOSE_METRICS = (
    "agentpy_turns_total",
    "agentpy_turn_duration_seconds",
    "agentpy_turn_tool_calls",
    "agentpy_turn_tokens",
)


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def _all_exprs(dashboard: dict) -> list[str]:
    out: list[str] = []
    for panel in dashboard.get("panels", []):
        for tgt in panel.get("targets", []) or []:
            if tgt.get("expr"):
                out.append(tgt["expr"])
    return out


def test_dashboard_json_parses() -> None:
    """The dashboard JSON file must be parseable."""
    payload = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert "panels" in payload
    assert isinstance(payload["panels"], list)
    assert len(payload["panels"]) > 0


def test_dashboard_uid_unchanged(dashboard: dict) -> None:
    """Regression guard: UID must stay 'agentpy-fleet' so URLs don't break."""
    assert dashboard.get("uid") == "agentpy-fleet"


def test_dashboard_has_no_top_level_id(dashboard: dict) -> None:
    """The numeric id is per-Grafana-instance and must not be in source control."""
    assert "id" not in dashboard or dashboard.get("id") is None


def test_dashboard_has_base_panels(dashboard: dict) -> None:
    """Each base metric is queried by at least one panel."""
    exprs = _all_exprs(dashboard)
    assert exprs, "dashboard has no panel targets"
    for metric in BASE_METRICS:
        assert any(metric in e for e in exprs), (
            f"no panel queries base metric {metric!r}"
        )


def test_dashboard_has_verbose_panels(dashboard: dict) -> None:
    """Each verbose-mode metric (issue #413 AC3) has a dedicated panel."""
    exprs = _all_exprs(dashboard)
    for metric in VERBOSE_METRICS:
        assert any(metric in e for e in exprs), (
            f"no panel queries verbose metric {metric!r}"
        )


def test_dashboard_panel_titles_unique(dashboard: dict) -> None:
    """Distinct titles help operators eyeball which panel is which."""
    titles = [p.get("title") for p in dashboard["panels"]]
    assert len(titles) == len(set(titles)), f"duplicate panel titles: {titles}"


class _FakePromHandler(http.server.BaseHTTPRequestHandler):
    """Returns one fake series for any /api/v1/query."""

    def log_message(self, *_a, **_k) -> None:  # silence the test output
        return

    def do_GET(self) -> None:
        if not self.path.startswith("/api/v1/query"):
            self.send_error(404)
            return
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"__name__": "fake"}, "value": [0, "1"]}
                    ],
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_prom():
    """Spin up a tiny HTTP server that returns one fake series per query."""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _FakePromHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_script_runs(fake_prom: str) -> None:
    """verify_dashboard.py must exit 0 against a Prom that returns one series."""
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--prom-url",
            fake_prom,
            str(DASHBOARD_PATH),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"verify_dashboard.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Each panel target should report OK against the fake Prom
    assert "OK" in result.stdout
    assert "FAIL" not in result.stdout
