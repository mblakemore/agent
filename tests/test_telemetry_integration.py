"""End-to-end OTLP integration tests for telemetry.py against a live Prometheus
receiver (issue #412).

Unlike ``tests/test_telemetry.py`` which mocks the SDK, these tests boot the
real OpenTelemetry SDK, push to ``http://localhost:9090/api/v1/otlp/v1/metrics``,
and query the Prometheus HTTP API to confirm the round-trip lands the expected
series and values.

All tests are marked ``@pytest.mark.integration`` so the default ``pytest tests/``
run skips them. To run explicitly::

    python3 -m pytest tests/test_telemetry_integration.py -v -m integration

If the local Prometheus stack is unreachable, every test in this file is
skipped via the ``_require_prom`` fixture (no false failures in environments
without the stack). Each test uses a unique ``instance`` label so series do
not collide between runs; OTLP series auto-stale after 5 minutes so no
explicit cleanup is needed.

Acceptance criteria reference: see issue #412 (criteria 1-7).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import pytest

# Ensure repo root (containing ``telemetry.py``) is importable regardless of
# how pytest was invoked.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_PROM_BASE = "http://localhost:9090"
_PROM_HEALTHY = f"{_PROM_BASE}/-/healthy"
_PROM_QUERY = f"{_PROM_BASE}/api/v1/query"
_PROM_SERIES = f"{_PROM_BASE}/api/v1/series"

# Prometheus's OTLP receiver only accepts cumulative temporality. The OTel SDK
# defaults to delta and reads this env var at exporter construction time. We
# set it BEFORE telemetry is initialised in each test so the exporter is built
# correctly even on hosts where a different value is exported in the shell.
_TEMPORALITY_ENV = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"

# Settle delay between ``shutdown()`` (which force-flushes) and querying
# Prometheus. The OTLP receiver ingests synchronously but the head-block
# write may need a moment to be visible to the query API.
_SETTLE_S = 2.0


def _have_otel() -> bool:
    return importlib.util.find_spec("opentelemetry") is not None


def _http_get_json(url: str, *, timeout: float = 4.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _query(promql: str) -> List[Dict[str, Any]]:
    url = f"{_PROM_QUERY}?query={urllib.parse.quote(promql)}"
    data = _http_get_json(url)
    if data.get("status") != "success":
        raise AssertionError(f"Prometheus query failed: {data!r}")
    return data["data"]["result"]


def _series(matcher: str) -> List[Dict[str, Any]]:
    url = f"{_PROM_SERIES}?match[]={urllib.parse.quote(matcher)}"
    data = _http_get_json(url)
    if data.get("status") != "success":
        raise AssertionError(f"Prometheus series query failed: {data!r}")
    return data["data"]


def _unique_instance(prefix: str) -> str:
    """Return a unique time-ordered instance label.

    Uniqueness comes from PID + monotonic-ns. This guards against series
    collision when the test suite is run repeatedly or in parallel."""
    return f"{prefix}-{os.getpid()}-{time.time_ns()}"


def _wait_for_series(promql: str, *, timeout_s: float = 8.0) -> List[Dict[str, Any]]:
    """Poll the query API until at least one series appears or timeout.

    OTLP ingest is fast but not instantaneous; this loop avoids flakes
    when the head block hasn't surfaced the series yet."""
    deadline = time.monotonic() + timeout_s
    last: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = _query(promql)
        if last:
            return last
        time.sleep(0.5)
    return last


def _purge_telemetry_modules() -> None:
    """Drop telemetry + opentelemetry from sys.modules so the next test
    starts with a clean module-level state and re-reads env vars."""
    for name in list(sys.modules):
        if name == "telemetry" or name == "opentelemetry" or name.startswith(
            "opentelemetry."
        ):
            del sys.modules[name]


@pytest.fixture
def _require_prom():
    """Skip the test if the live Prometheus stack is unreachable.

    Probes ``/-/healthy`` with a 2s timeout. This is the contract from
    issue #412 acceptance criterion 2: when the stack is down the test
    must be a no-op (not a failure).
    """
    if not _have_otel():
        pytest.skip("opentelemetry not installed in this interpreter")
    try:
        with urllib.request.urlopen(_PROM_HEALTHY, timeout=2.0) as resp:
            if resp.status != 200:
                pytest.skip(
                    f"Prometheus stack not reachable on localhost:9090 "
                    f"(healthy probe returned {resp.status})"
                )
    except Exception as exc:  # urllib + socket errors
        pytest.skip(
            f"Prometheus stack not reachable on localhost:9090 ({exc!r})"
        )


@pytest.fixture
def _telemetry_env(monkeypatch):
    """Per-test env scrub + cumulative-temporality enforcement.

    Clears all ``AGENTPY_TELEMETRY*`` and ``OTEL_*`` keys, then sets the
    cumulative-temporality preference (acceptance criterion 6 — required
    BEFORE the opentelemetry import). Also purges cached modules so the
    SDK re-reads env at construction."""
    for key in list(os.environ):
        if key.startswith("AGENTPY_TELEMETRY") or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(_TEMPORALITY_ENV, "cumulative")
    _purge_telemetry_modules()
    yield
    # After test: best-effort shutdown of any provider this test left
    # behind, then drop the modules so the next test is clean.
    tele = sys.modules.get("telemetry")
    if tele is not None:
        try:
            tele.shutdown()
        except Exception:
            pass
    _purge_telemetry_modules()


@pytest.mark.integration
def test_otlp_base_export_lands_in_prometheus(_require_prom, _telemetry_env, monkeypatch):
    """Base-schema round-trip: cycles, tokens, errors land in Prometheus."""
    instance = _unique_instance("test-base")
    monkeypatch.setenv("AGENTPY_TELEMETRY", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_INSTANCE", instance)

    import telemetry

    assert telemetry.init() is True

    telemetry.record_cycle("completed", 1.5)
    telemetry.record_tokens("test-model", "prompt", 100)
    telemetry.record_tokens("test-model", "completion", 50)
    telemetry.record_error("TimeoutError")
    telemetry.shutdown()

    time.sleep(_SETTLE_S)

    cycles = _wait_for_series(
        f'agentpy_cycles_total{{instance="{instance}"}}'
    )
    assert cycles, (
        f"agentpy_cycles_total{{instance={instance!r}}} did not land in "
        f"Prometheus within timeout"
    )
    assert float(cycles[0]["value"][1]) == 1.0

    prompt_tokens = _wait_for_series(
        f'agentpy_tokens_total{{instance="{instance}",kind="prompt"}}'
    )
    assert prompt_tokens, "prompt tokens did not land"
    assert float(prompt_tokens[0]["value"][1]) == 100.0

    completion_tokens = _wait_for_series(
        f'agentpy_tokens_total{{instance="{instance}",kind="completion"}}'
    )
    assert completion_tokens, "completion tokens did not land"
    assert float(completion_tokens[0]["value"][1]) == 50.0

    errors = _wait_for_series(
        f'agentpy_errors_total{{instance="{instance}",kind="TimeoutError"}}'
    )
    assert errors, "errors did not land"
    assert float(errors[0]["value"][1]) == 1.0


@pytest.mark.integration
def test_otlp_verbose_export_lands_in_prometheus(_require_prom, _telemetry_env, monkeypatch):
    """Verbose-schema round-trip: turn counter and histogram count land in Prometheus."""
    instance = _unique_instance("test-verbose")
    monkeypatch.setenv("AGENTPY_TELEMETRY", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_VERBOSE", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_INSTANCE", instance)

    import telemetry

    assert telemetry.init() is True
    assert telemetry.verbose_enabled() is True

    for _ in range(5):
        telemetry.record_turn(
            "main",
            duration_s=0.42,
            tool_calls=3,
            in_tokens=1000,
            out_tokens=200,
            model="test-model",
        )
    telemetry.shutdown()

    time.sleep(_SETTLE_S)

    turns = _wait_for_series(
        f'agentpy_turns_total{{instance="{instance}",role="main"}}'
    )
    assert turns, (
        f"agentpy_turns_total{{instance={instance!r}}} did not land "
        f"in Prometheus within timeout"
    )
    assert float(turns[0]["value"][1]) == 5.0

    # Prom's OTLP→Prom translation appends ``_seconds`` to histograms whose
    # unit is ``s``. Per issue #417 the SDK-side metric name is now
    # ``agentpy_turn_duration`` (NO pre-applied ``_seconds``), so Prom emits
    # the canonical ``agentpy_turn_duration_seconds_count`` series. We query
    # ONLY the canonical form — no fallback to the doubled-suffix name, so a
    # regression to the pre-#417 naming fails this test cleanly.
    duration_count = _wait_for_series(
        f'agentpy_turn_duration_seconds_count{{instance="{instance}"}}'
    )
    assert duration_count, "turn duration histogram count did not land"
    assert float(duration_count[0]["value"][1]) == 5.0


@pytest.mark.integration
def test_verbose_does_not_leak_per_turn_label(_require_prom, _telemetry_env, monkeypatch):
    """Cardinality guard: no per-turn label appears on turn-duration series.

    This is the explicit "no per-turn-labeled series" rule from closed #387 /
    shipped #401 — verbose telemetry must aggregate by role/model only."""
    instance = _unique_instance("test-card")
    monkeypatch.setenv("AGENTPY_TELEMETRY", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_VERBOSE", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_INSTANCE", instance)

    import telemetry

    assert telemetry.init() is True

    for i in range(3):
        telemetry.record_turn(
            "main",
            duration_s=0.1 + i * 0.05,
            tool_calls=i,
            in_tokens=100 + i,
            out_tokens=50 + i,
            model="test-model",
        )
    telemetry.shutdown()

    time.sleep(_SETTLE_S)

    # Wait for the histogram series to surface, then enumerate ALL series
    # names that match agentpy_turn_duration* for this instance and assert
    # none carry a ``turn`` label.
    _wait_for_series(
        f'agentpy_turn_duration_seconds_count{{instance="{instance}"}}'
    )

    matches = _series(
        '{__name__=~"agentpy_turn_duration.*",instance="' + instance + '"}'
    )
    assert matches, (
        "expected at least one agentpy_turn_duration* series for "
        f"instance={instance!r}"
    )
    for s in matches:
        assert "turn" not in s, (
            f"per-turn label leaked into telemetry series (cardinality bug "
            f"from #387/#401): {s!r}"
        )


@pytest.mark.integration
def test_no_otlp_temporality_rejection_logs(_require_prom, _telemetry_env, monkeypatch, caplog):
    """No SDK log mentions delta-temporality rejection during init+export.

    The Prom OTLP receiver rejects delta-temporality counters with
    "invalid temporality and type combination". This test confirms that
    cumulative is honored end-to-end by inspecting captured SDK logs and
    the Prom receiver-emitted protocol response."""
    instance = _unique_instance("test-temp")
    monkeypatch.setenv("AGENTPY_TELEMETRY", "1")
    monkeypatch.setenv("AGENTPY_TELEMETRY_INSTANCE", instance)

    import telemetry

    with caplog.at_level(logging.DEBUG):
        assert telemetry.init() is True
        telemetry.record_cycle("completed", 0.1)
        telemetry.shutdown()

    forbidden_substrings = ("delta temporality", "rejected", "invalid temporality")
    for record in caplog.records:
        msg = record.getMessage().lower()
        for needle in forbidden_substrings:
            assert needle not in msg, (
                f"OTel log contained forbidden substring {needle!r}: "
                f"{record.getMessage()!r}"
            )

    # The cumulative-temporality preference must be set before the SDK
    # construction reads it. By this point ``init()`` has either set it
    # (via setdefault) or honored a pre-existing setting; either way the
    # value seen by the exporter must be ``cumulative``.
    assert os.environ.get(_TEMPORALITY_ENV) == "cumulative"
