import os
import socket
import logging

logger = logging.getLogger(__name__)

# Global state
_provider = None
_meter = None
_metrics = {}
_enabled = False

def init() -> bool:
    """
    Initializes OTLP telemetry if AGENTPY_TELEMETRY is enabled.
    Returns True if enabled, False otherwise.
    """
    global _provider, _meter, _metrics, _enabled
    
    val = os.environ.get("AGENTPY_TELEMETRY", "").lower()
    if val not in ("1", "true", "yes"):
        _enabled = False
        return False

    try:
        # MANDATORY: Set temporality BEFORE importing opentelemetry
        os.environ["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "cumulative"
        
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource

        # Config
        endpoint = os.environ.get("AGENTPY_TELEMETRY_OTLP_ENDPOINT", "http://localhost:9090/api/v1/otlp/v1/metrics")
        job = os.environ.get("AGENTPY_TELEMETRY_JOB", "agentpy")
        instance = os.environ.get("AGENTPY_TELEMETRY_INSTANCE", f"{socket.gethostname()}-{os.getpid()}")

        # Resource setup
        resource = Resource.create({
            "service.name": job,
            "service.instance.id": instance,
        })

        # Exporter and Reader
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15_000)
        
        _provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(_provider)
        _meter = metrics.get_meter("agentpy")

        # Base Metric Schema
        # Counters: Note that OTel SDK adds _total automatically in Prom translation
        # for some configurations, but the issue specifies creating them WITHOUT _total.
        _metrics["up"] = _meter.create_observable_gauge(
            "agentpy_up", 
            callbacks=[lambda options: [metrics.Observation(1)]]
        )
        _metrics["last_seen"] = _meter.create_observable_gauge(
            "agentpy_last_seen_timestamp_seconds", 
            callbacks=[lambda options: [metrics.Observation(os.times().elapsed)]]
        )
        _metrics["cycles"] = _meter.create_counter(
            "agentpy_cycles", 
            description="Total number of cycles completed"
        )
        _metrics["tokens"] = _meter.create_counter(
            "agentpy_tokens", 
            description="Total tokens used"
        )
        _metrics["errors"] = _meter.create_counter(
            "agentpy_errors", 
            description="Total errors encountered"
        )

        _enabled = True
        return True

    except ImportError as e:
        logger.error(f"telemetry.disabled missing_packages=opentelemetry-sdk,opentelemetry-exporter-otlp-proto-http install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http")
        _enabled = False
        return False
    except Exception as e:
        logger.error(f"Failed to initialize telemetry: {e}")
        _enabled = False
        return False

def verbose_enabled() -> bool:
    return _enabled and os.environ.get("AGENTPY_TELEMETRY_VERBOSE", "").lower() in ("1", "true", "yes")

def record_cycle(status: str, duration_s: float):
    if not _enabled: return
    # status is a label: 'completed' or 'failed'
    _metrics["cycles"].add(1, {"status": status})
    # Note: cycle_duration_seconds is a histogram in the plan. 
    # For the base cycle, we use a simple counter or we can add a histogram here.
    # The plan says: agentpy_cycle_duration_seconds (Histogram). 
    # Let's add it to the base schema if we are doing base wiring.
    if "cycle_duration" not in _metrics:
        _metrics["cycle_duration"] = _meter.create_histogram(
            "agentpy_cycle_duration_seconds", 
            unit="s"
        )
    _metrics["cycle_duration"].record(duration_s)

def record_tokens(model: str, kind: str, n: int):
    if not _enabled: return
    _metrics["tokens"].add(n, {"model": model, "kind": kind})

def record_error(kind: str):
    if not _enabled: return
    _metrics["errors"].add(1, {"kind": kind})

def shutdown():
    global _provider
    if _provider:
        _provider.shutdown()
        _provider = None
