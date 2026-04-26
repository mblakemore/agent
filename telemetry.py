import os
import socket
import atexit
import logging
import time

# Global state to avoid re-initializing
_provider = None
_meter = None
_enabled = False
_verbose = False

# Metrics
_m_up = None
_m_last_seen = None
_m_cycles = None
_m_tokens = None
_m_errors = None
_m_cycle_duration = None

# Verbose Metrics
_m_turns = None
_m_turn_duration = None
_m_turn_tool_calls = None
_m_turn_tokens = None

logger = logging.getLogger("agent.telemetry")

def init() -> bool:
    """
    Initialize OTLP telemetry.
    Returns True if telemetry is enabled and successfully initialized.
    """
    global _provider, _meter, _enabled, _verbose, _m_up, _m_last_seen, _m_cycles, _m_tokens, _m_errors, _m_cycle_duration
    
    # 1. Check if enabled
    val = os.environ.get("AGENTPY_TELEMETRY", "").lower()
    if val not in ("1", "true", "yes"):
        _enabled = False
        return False
    
    # 2. Check verbose flag
    _verbose = os.environ.get("AGENTPY_TELEMETRY_VERBOSE", "").lower() in ("1", "true", "yes")
    
    # 3. Set cumulative temporality BEFORE imports (AC 4)
    os.environ["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "cumulative"
    
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource
    except ImportError as e:
        logger.error(f"telemetry.disabled missing_packages=opentelemetry-sdk,opentelemetry-exporter-otlp-proto-http install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http")
        _enabled = False
        return False

    try:
        # 4. Configure Resource (AC 3)
        job = os.environ.get("AGENTPY_TELEMETRY_JOB", "agentpy")
        instance = os.environ.get("AGENTPY_TELEMETRY_INSTANCE")
        if not instance:
            instance = f"{socket.gethostname()}-{os.getpid()}"
            
        resource = Resource.create({
            "service.name": job,
            "service.instance.id": instance,
        })
        
        # 5. Setup Exporter & Provider
        endpoint = os.environ.get("AGENTPY_TELEMETRY_OTLP_ENDPOINT", "http://localhost:9090/api/v1/otlp/v1/metrics")
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15_000)
        
        _provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(_provider)
        _meter = metrics.get_meter("agent.py")
        
        # 6. Create Base Metrics (AC 5)
        # Note: Prom appends _total to counters, so we don't add it here.
        _m_up = _meter.create_observable_gauge(
            "agentpy_up", 
            callbacks=[lambda options: [metrics.Observation(1)]]
        )
        _m_last_seen = _meter.create_observable_gauge(
            "agentpy_last_seen_timestamp_seconds",
            callbacks=[lambda options: [metrics.Observation(time.time())]]
        )
        _m_cycles = _meter.create_counter(
            "agentpy_cycles",
            description="Total number of agent cycles completed"
        )
        _m_cycle_duration = _meter.create_histogram(
            "agentpy_cycle_duration_seconds",
            unit="s",
            description="Duration of agent cycles"
        )
        _m_tokens = _meter.create_counter(
            "agentpy_tokens",
            description="Total tokens consumed"
        )
        _m_errors = _meter.create_counter(
            "agentpy_errors",
            description="Total errors encountered"
        )
        
        # 7. Verbose Metrics (AC 6 - partially implemented as placeholders)
        if _verbose:
            _m_turns = _meter.create_counter("agentpy_turns", description="Total turns")
            _m_turn_duration = _meter.create_histogram("agentpy_turn_duration_seconds", unit="s")
            _m_turn_tool_calls = _meter.create_histogram("agentpy_turn_tool_calls")
            _m_turn_tokens = _meter.create_histogram("agentpy_turn_tokens")
            
        _enabled = True
        return True
    except Exception as e:
        logger.exception(f"Failed to initialize telemetry: {e}")
        _enabled = False
        return False

def verbose_enabled() -> bool:
    return _verbose

def record_cycle(status: str, duration_s: float):
    """Record cycle completion."""
    if not _enabled:
        return
    try:
        _m_cycles.add(1, {"status": status})
        _m_cycle_duration.record(duration_s)
    except Exception:
        pass

def record_tokens(model: str, kind: str, n: int):
    """Record token spend."""
    if not _enabled or _m_tokens is None:
        return
    try:
        _m_tokens.add(n, {"model": model, "kind": kind})
    except Exception:
        pass

def record_error(kind: str):
    """Record an error occurrence."""
    if not _enabled or _m_errors is None:
        return
    try:
        _m_errors.add(1, {"kind": kind})
    except Exception:
        pass

def record_turn(role: str, duration_s: float, tool_calls: int, in_tokens: int, out_tokens: int, model: str):
    """Record turn details (verbose only)."""
    if not _enabled or not _verbose:
        return
    try:
        _m_turns.add(1, {"role": role})
        _m_turn_duration.record(duration_s)
        _m_turn_tool_calls.record(tool_calls)
        _m_turn_tokens.record(in_tokens + out_tokens)
    except Exception:
        pass

def shutdown():
    """Flush and shutdown the telemetry provider."""
    global _provider
    if _provider:
        try:
            _provider.shutdown()
        except Exception:
            pass
        finally:
            _provider = None
