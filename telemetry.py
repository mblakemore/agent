"""OTLP telemetry module for agent.py (env-flag opt-in, lazy import).

This module exposes a small public API that the rest of agent.py can call
unconditionally; when ``AGENTPY_TELEMETRY`` is not enabled all functions are
no-ops and ``opentelemetry`` is never imported.

Wiring into ``agent.py`` is out of scope here (sub-issue 2). See
``plan/AGENTPY_PROMETHEUS_HOOKUP.md`` for the Prometheus/OTLP background and
the cumulative-temporality requirement that Prometheus's OTLP receiver imposes.

Counter names intentionally have NO ``_total`` suffix — Prometheus's OTLP
translation appends it automatically; double-suffixing breaks the dashboard.

By the same rule, histogram names MUST NOT pre-apply a unit suffix
(``_seconds``, ``_bytes``, etc.) — Prom appends one based on the OTel
``unit=`` argument. Pre-applying produces ``..._seconds_seconds_*`` in
Prom's catalog (see issue #417).
"""

from __future__ import annotations

import logging
import os
import socket
import time
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Module-level state. Populated by ``init()`` when telemetry is enabled.
# Kept as plain globals (not a class) so callers can do cheap ``is None``
# checks in hot paths without an attribute lookup.
_provider: Any = None
_meter: Any = None
_enabled: bool = False
_verbose: bool = False

# Counters / histograms. Created in ``init()`` when base is enabled.
_cycles: Any = None
_tokens: Any = None
_errors: Any = None
_cycle_duration: Any = None
_tool_calls: Any = None
_tool_errors: Any = None
_hallucinations: Any = None
_summaries: Any = None

# Verbose-only meters. Created only when verbose is enabled.
_turns: Any = None
_turn_duration: Any = None
_turn_tool_calls: Any = None
_turn_tokens: Any = None

_INSTALL_HINT = (
    "telemetry: AGENTPY_TELEMETRY is set but opentelemetry is not installed. "
    "Install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
)

_DEFAULT_OTLP_ENDPOINT = "http://localhost:9090/api/v1/otlp/v1/metrics"
_DEFAULT_JOB = "agentpy"
_EXPORT_INTERVAL_MS = 15_000


def _truthy(val: Optional[str]) -> bool:
    """Return True for canonical truthy env values."""
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _default_instance() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def init() -> bool:
    """Initialise telemetry if ``AGENTPY_TELEMETRY`` is enabled.

    Returns ``True`` when the OTLP MeterProvider was created successfully, and
    ``False`` otherwise (either because telemetry is opt-out, or because the
    optional dependencies are missing). When telemetry is disabled, no
    ``opentelemetry`` import is performed.
    """
    global _provider, _meter, _enabled, _verbose
    global _cycles, _tokens, _errors, _cycle_duration
    global _turns, _turn_duration, _turn_tool_calls, _turn_tokens
    global _tool_calls, _tool_errors, _hallucinations, _summaries, _context_size

    if not _truthy(os.environ.get("AGENTPY_TELEMETRY")):
        _enabled = False
        return False

    # Prometheus's OTLP receiver only accepts cumulative temporality; OTel
    # SDK defaults to delta. Set this BEFORE importing opentelemetry so the
    # SDK picks it up at module load time. We HARD-SET (not setdefault) — a
    # hostile shell-level ``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE``
    # export (e.g. ``delta`` from an unrelated OTel deployment) would
    # otherwise silently break the Prom receiver path: HTTP 200, but the
    # receiver discards the payload as "invalid temporality and type
    # combination" with no exporter-side error. See issue #416.
    os.environ["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "cumulative"

    try:
        # Lazy imports — these only happen when telemetry is enabled.
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
    except ImportError as exc:
        logger.error("%s (%s)", _INSTALL_HINT, exc)
        _enabled = False
        return False

    endpoint = os.environ.get(
        "AGENTPY_TELEMETRY_OTLP_ENDPOINT", _DEFAULT_OTLP_ENDPOINT
    )
    job = os.environ.get("AGENTPY_TELEMETRY_JOB", _DEFAULT_JOB)
    instance = os.environ.get("AGENTPY_TELEMETRY_INSTANCE") or _default_instance()

    resource = Resource.create(
        {
            "service.name": job,
            "service.instance.id": instance,
        }
    )
    exporter = OTLPMetricExporter(endpoint=endpoint)
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=_EXPORT_INTERVAL_MS
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter("agentpy")

    # Silence the OTel SDK's noisy export-retry loggers so that transient
    # endpoint unavailability (e.g. Prometheus not running) does not spam
    # stderr.  These failures are expected and non-actionable at the terminal;
    # set CRITICAL so only truly fatal events surface.
    for _noisy_logger in (
        "opentelemetry.sdk.metrics._internal.export",
        "opentelemetry.exporter.otlp.proto.http",
        "opentelemetry",
    ):
        logging.getLogger(_noisy_logger).setLevel(logging.CRITICAL)

    # New metrics for tool execution and agent health.
    _tool_calls = meter.create_counter(
        "agentpy_tool_calls",
        description="tool invocations by tool name",
    )
    _tool_errors = meter.create_counter(
        "agentpy_tool_errors",
        description="tool invocation errors by tool name",
    )
    _hallucinations = meter.create_counter(
        "agentpy_hallucinations",
        description="hallucination guard firings",
    )
    _summaries = meter.create_counter(
        "agentpy_summaries",
        description="context summarization events",
    )
    _context_size = meter.create_histogram(
        "agentpy_context_size",
        description="current context window size in tokens",
        unit="tokens",
    )
    # Base meters — always created when telemetry is enabled.
    # NOTE: NO `_total` suffix — Prom appends it during OTLP translation.
    _cycles = meter.create_counter("agentpy_cycles", description="cycles run")
    _tokens = meter.create_counter("agentpy_tokens", description="tokens consumed")
    _errors = meter.create_counter("agentpy_errors", description="errors raised")
    # Histogram name does NOT carry the ``_seconds`` suffix — Prom's
    # OTLP→Prom translation appends it from ``unit="s"``. Pre-applying
    # ``_seconds`` produces the doubled ``..._seconds_seconds_*`` series
    # surfaced in issue #417.
    _cycle_duration = meter.create_histogram(
        "agentpy_cycle_duration",
        description="cycle wallclock duration",
        unit="s",
    )

    def _up_callback(_options):
        from opentelemetry.metrics import Observation
        yield Observation(1)

    def _last_seen_callback(_options):
        from opentelemetry.metrics import Observation
        yield Observation(time.time())

    meter.create_observable_gauge(
        "agentpy_up",
        callbacks=[_up_callback],
        description="agent process up",
    )
    meter.create_observable_gauge(
        "agentpy_last_seen_timestamp_seconds",
        callbacks=[_last_seen_callback],
        description="last heartbeat",
    )

    _provider = provider
    _meter = meter
    _enabled = True

    # Verbose meters — only created when verbose is enabled, to keep
    # cardinality bounded in default deployments.
    if _truthy(os.environ.get("AGENTPY_TELEMETRY_VERBOSE")):
        _verbose = True
        _turns = meter.create_counter("agentpy_turns", description="LLM turns")
        # Same idiom as ``_cycle_duration`` — name MUST NOT pre-apply
        # ``_seconds``; Prom appends it from ``unit="s"`` (see #417).
        _turn_duration = meter.create_histogram(
            "agentpy_turn_duration",
            description="LLM turn wallclock duration",
            unit="s",
        )
        _turn_tool_calls = meter.create_histogram(
            "agentpy_turn_tool_calls",
            description="tool calls per LLM turn",
        )
        _turn_tokens = meter.create_histogram(
            "agentpy_turn_tokens",
            description="tokens per LLM turn",
        )
    else:
        _verbose = False

    return True


def verbose_enabled() -> bool:
    """True iff base telemetry is on AND ``AGENTPY_TELEMETRY_VERBOSE`` is on."""
    return bool(_enabled and _verbose)


def record_cycle(status: str, duration_s: float) -> None:
    """Record a cycle completion. No-op when telemetry is disabled."""
    if not _enabled or _cycles is None:
        return
    try:
        _cycles.add(1, {"status": status})
        if _cycle_duration is not None:
            _cycle_duration.record(float(duration_s))
    except Exception:  # never let telemetry break the agent
        logger.debug("telemetry.record_cycle failed", exc_info=True)


def record_tokens(model: str, kind: str, n: int) -> None:
    """Record token usage. No-op when telemetry is disabled."""
    if not _enabled or _tokens is None:
        return
    try:
        _tokens.add(int(n), {"model": model, "kind": kind})
    except Exception:
        logger.debug("telemetry.record_tokens failed", exc_info=True)


def record_error(kind: str) -> None:
    """Record an error. No-op when telemetry is disabled."""
    if not _enabled or _errors is None:
        return
    try:
        _errors.add(1, {"kind": kind})
    except Exception:
        logger.debug("telemetry.record_error failed", exc_info=True)


def record_turn(
    role: str,
    duration_s: float,
    tool_calls: int,
    in_tokens: int,
    out_tokens: int,
    model: str,
) -> None:
    """Record per-turn metrics. No-op unless verbose telemetry is enabled."""
    if not verbose_enabled() or _turns is None:
        return
    try:
        attrs = {"role": role, "model": model}
        _turns.add(1, attrs)
        if _turn_duration is not None:
            _turn_duration.record(float(duration_s), attrs)
        if _turn_tool_calls is not None:
            _turn_tool_calls.record(int(tool_calls), attrs)
        if _turn_tokens is not None:
            _turn_tokens.record(int(in_tokens), {**attrs, "kind": "input"})
            _turn_tokens.record(int(out_tokens), {**attrs, "kind": "output"})
    except Exception:
        logger.debug("telemetry.record_turn failed", exc_info=True)


def shutdown() -> None:
    """Flush and shut down the MeterProvider if one was created."""
    global _provider
    if _provider is None:
        return
    try:
        def _do_shutdown():
            try:
                _provider.shutdown()
            except Exception:
                logger.debug("telemetry.shutdown failed", exc_info=True)

        t = threading.Thread(target=_do_shutdown, daemon=True)
        t.start()
        t.join(timeout=2.0)
    except Exception:
        logger.debug("telemetry.shutdown failed", exc_info=True)
    finally:
        _provider = None


def record_tool_call(name: str) -> None:
    """Record a tool call by name. No-op when telemetry is disabled."""
    if not _enabled or _tool_calls is None:
        return
    try:
        _tool_calls.add(1, {"tool": name})
    except Exception:
        logger.debug("telemetry.record_tool_call failed", exc_info=True)

def record_tool_error(name: str, kind: str) -> None:
    """Record a tool error by name and kind. No-op when telemetry is disabled."""
    if not _enabled or _tool_errors is None:
        return
    try:
        _tool_errors.add(1, {"tool": name, "kind": kind})
    except Exception:
        logger.debug("telemetry.record_tool_error failed", exc_info=True)

def record_hallucination() -> None:
    """Record a hallucination guard firing. No-op when telemetry is disabled."""
    if not _enabled or _hallucinations is None:
        return
    try:
        _hallucinations.add(1)
    except Exception:
        logger.debug("telemetry.record_hallucination failed", exc_info=True)

def record_summary() -> None:
    """Record a context summarization event. No-op when telemetry is disabled."""
    if not _enabled or _summaries is None:
        return
    try:
        _summaries.add(1)
    except Exception:
        logger.debug("telemetry.record_summary failed", exc_info=True)
