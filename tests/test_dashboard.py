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


# ──────────────────────────────────────────────────────────────────────
# Issue #421 — instance template variable + per-panel filter
# ──────────────────────────────────────────────────────────────────────

import re


def _walk_targets(dashboard: dict):
    """Yield every (panel_title, expr) pair from the dashboard, including
    panels nested under rows."""
    for panel in dashboard.get("panels", []) or []:
        title = panel.get("title", "<untitled>")
        for tgt in panel.get("targets", []) or []:
            expr = tgt.get("expr")
            if expr:
                yield title, expr
        for sub in panel.get("panels", []) or []:
            sub_title = sub.get("title", "<untitled>")
            for tgt in sub.get("targets", []) or []:
                expr = tgt.get("expr")
                if expr:
                    yield sub_title, expr


def test_dashboard_has_instance_template_variable(dashboard: dict) -> None:
    """Issue #421 AC1: the dashboard exposes an `instance` query template var.

    The variable must drive PromQL via $instance and default to "all
    instances" so existing URLs without ?var-instance= keep working.
    """
    templating = dashboard.get("templating", {})
    var_list = templating.get("list", [])
    assert isinstance(var_list, list) and var_list, (
        "templating.list is empty — instance variable not defined"
    )
    by_name = {v.get("name"): v for v in var_list}
    assert "instance" in by_name, (
        f"no template variable named 'instance' (have {list(by_name)})"
    )
    var = by_name["instance"]
    assert var.get("type") == "query", f"variable type must be 'query', got {var.get('type')}"
    # Either dict-shaped query or legacy string form is acceptable.
    q = var.get("query")
    if isinstance(q, dict):
        q_text = q.get("query", "")
    else:
        q_text = q or ""
    assert "label_values" in q_text and "agentpy_up" in q_text and "instance" in q_text, (
        f"variable query should label_values agentpy_up by instance, got {q_text!r}"
    )
    assert var.get("multi") is True, "instance var should support multi-select"
    assert var.get("includeAll") is True, "instance var should expose an All option"
    assert var.get("allValue") == ".+", (
        "instance var allValue should be the regex '.+' so PromQL `=~\"$instance\"` "
        "matches every series when 'All' is selected"
    )


def test_every_panel_filters_by_instance_var(dashboard: dict) -> None:
    """Issue #421 AC2: every PromQL expr referencing an agentpy_* metric
    must include the `instance=~"$instance"` matcher so the dropdown
    actually filters the panel.
    """
    metric_re = re.compile(r"agentpy_[A-Za-z0-9_]+")
    # Match `agentpy_<name>{...instance=~"$instance"...}` (may have other labels).
    filter_re = re.compile(
        r'agentpy_[A-Za-z0-9_]+\s*\{[^}]*instance\s*=~\s*"\$instance"[^}]*\}'
    )
    failures: list[str] = []
    for title, expr in _walk_targets(dashboard):
        if not metric_re.search(expr):
            continue  # skip panels that don't query an agentpy_* metric
        # Every distinct agentpy_* metric reference in this expr must carry
        # the instance filter.
        # Simpler check: each agentpy_* reference must be immediately followed
        # by a `{...}` containing `instance=~"$instance"`.
        for m in metric_re.finditer(expr):
            metric_start = m.start()
            tail = expr[metric_start:]
            if not filter_re.match(tail):
                failures.append(f"[{title}] missing instance filter in: {expr!r}")
                break
    assert not failures, (
        "panels without instance-var filter:\n  " + "\n  ".join(failures)
    )
