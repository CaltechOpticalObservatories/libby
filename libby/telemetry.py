"""Optional OpenTelemetry traces and metrics for libby peers.

Everything here is a no-op until :func:`configure` succeeds, which needs the
``libby[otel]`` extra and an OTLP endpoint set through the standard ``OTEL_*``
environment variables. Without both, libby never imports OpenTelemetry.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from contextlib import ExitStack
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

RPC_SYSTEM = "libby"
ERROR_RESPONSE = "error_response"
DURATION_BUCKETS_S = (
    0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01,
    0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
_ENDPOINT_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)

Handler = Callable[[dict, dict], Optional[dict]]
Listener = Callable[[Any], None]


class _Instruments:
    """Tracer, histograms and OTel API modules, bound once telemetry is on."""

    def __init__(self) -> None:
        # Deferred: OpenTelemetry is the optional libby[otel] extra
        # pylint: disable=import-outside-toplevel
        from opentelemetry import metrics, trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        self.trace = trace
        self.metrics = metrics
        # bamboo's envelope fields are W3C Trace Context whatever OTEL_PROPAGATORS says
        self.propagator = TraceContextTextMapPropagator()
        self.tracer = trace.get_tracer("libby")
        meter = metrics.get_meter("libby")
        self.client_duration = meter.create_histogram(
            "rpc.client.call.duration", unit="s",
            description="Duration of libby requests this peer sent",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        )
        self.server_duration = meter.create_histogram(
            "rpc.server.call.duration", unit="s",
            description="Duration of libby requests this peer handled",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        )

    def incoming(self, traceparent: Optional[str], tracestate: Optional[str]) -> Any:
        """Return the context a received envelope's trace fields describe."""
        if not traceparent:
            return None
        carrier = {"traceparent": traceparent}
        if tracestate:
            carrier["tracestate"] = tracestate
        return self.propagator.extract(carrier)

    def finish(self, span: Any, error_type: Optional[str], attributes: Dict[str, Any],
               histogram: Any, started: float) -> None:
        """Mark a failed span and record the call's duration, if it is timed."""
        if error_type is not None:
            span.set_status(self.trace.StatusCode.ERROR)
            span.set_attribute("error.type", error_type)
            attributes = {**attributes, "error.type": error_type}
        if histogram is not None:
            histogram.record(time.perf_counter() - started, attributes)


# OTel providers are process-global, so the switch for using them is too
_instruments: Optional[_Instruments] = None  # pylint: disable=invalid-name


def export_requested() -> bool:
    """Return whether the environment names an OTLP endpoint to export to."""
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return any(os.environ.get(name) for name in _ENDPOINT_VARIABLES)


def enabled() -> bool:
    """Return whether libby is recording spans and metrics."""
    return _instruments is not None


def configure(service_name: str, service_namespace: Optional[str] = None, *,
              export_timeout_s: Optional[float] = None,
              export_metrics: bool = True) -> bool:
    """Turn on tracing and metrics for this process if export is requested.

    Install the OTLP SDK unless the application already installed its own
    providers, in which case libby records into those. The OTel API allows one
    provider per process, so the SDK stays installed until the process exits
    and later calls only report whether telemetry is on. Short-lived processes
    pass ``export_metrics=False``: each run is a new ``service.instance.id``,
    so its metrics are new series.
    """
    if _instruments is not None:
        return True
    if not export_requested():
        return False
    try:
        # Deferred: OpenTelemetry is the optional libby[otel] extra
        # pylint: disable=import-outside-toplevel
        from opentelemetry import trace
        if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            _install_sdk(service_name, service_namespace, export_timeout_s, export_metrics)
    except ImportError:
        logger.warning("an OTLP endpoint is set but libby[otel] is not installed; "
                       "telemetry stays off")
        return False
    use_current_providers()
    return True


def flush() -> None:
    """Export pending spans and metrics now, e.g. before a daemon stops."""
    instruments = _instruments
    if instruments is None:
        return
    for provider in (instruments.trace.get_tracer_provider(),
                     instruments.metrics.get_meter_provider()):
        # API-only providers have nothing to flush
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()


def _install_sdk(service_name: str, service_namespace: Optional[str],
                 export_timeout_s: Optional[float], export_metrics: bool) -> None:
    # pylint: disable=import-outside-toplevel
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = _resource(service_name, service_namespace)
    timeout = {} if export_timeout_s is None else {"timeout": export_timeout_s}

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**timeout)))
    trace.set_tracer_provider(tracer_provider)
    if export_metrics:
        _install_meter(resource, timeout)


def _install_meter(resource: Any, timeout: Dict[str, float]) -> None:
    # pylint: disable=import-outside-toplevel
    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(**timeout))],
    ))


def _resource(service_name: str, service_namespace: Optional[str]) -> Any:
    from opentelemetry.sdk.resources import Resource  # pylint: disable=import-outside-toplevel

    attributes = {"service.name": service_name, "host.name": socket.gethostname()}
    if service_namespace:
        attributes["service.namespace"] = service_namespace
    return Resource.create(attributes)


def use_current_providers() -> None:
    """Record into the providers already set on the OTel API, e.g. an app's own SDK."""
    global _instruments  # pylint: disable=global-statement
    _instruments = _Instruments()


def disable() -> None:
    """Stop recording; spans and metrics become no-ops again."""
    global _instruments  # pylint: disable=global-statement
    _instruments = None


class Call:
    """Trace context for one outgoing message, and the call's outcome."""

    __slots__ = ("traceparent", "tracestate", "error_type", "_span", "_stack", "_finish")

    def __init__(self) -> None:
        self.traceparent: Optional[str] = None
        self.tracestate: Optional[str] = None
        self.error_type: Optional[str] = None
        self._span: Any = None
        self._stack: Optional[ExitStack] = None
        self._finish: Callable[[Any, Optional[str]], None] = lambda *_: None

    @classmethod
    def start(cls, instruments: _Instruments, name: str, kind: Any,
              attributes: Dict[str, Any], histogram: Any = None) -> "Call":
        """Open a current span named ``name`` and capture its trace context."""
        call = cls()
        started = time.perf_counter()
        call._stack = ExitStack()
        call._span = call._stack.enter_context(instruments.tracer.start_as_current_span(
            name, kind=kind, attributes=attributes,
            record_exception=False, set_status_on_exception=False,
        ))
        call._finish = lambda span, error_type: instruments.finish(
            span, error_type, attributes, histogram, started)
        carrier: Dict[str, str] = {}
        instruments.propagator.inject(carrier)
        call.traceparent = carrier.get("traceparent")
        call.tracestate = carrier.get("tracestate")
        return call

    def __enter__(self) -> "Call":
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        if self._stack is None:
            return False
        if exc is not None:
            self.error_type = exc_type.__qualname__
            self._span.record_exception(exc)
        self._finish(self._span, self.error_type)
        self._stack.close()
        return False

    def check_request(self, result: Dict[str, Any]) -> None:
        """Classify a bamboo ``request_peer`` result as success or error."""
        if self._stack is None:
            return
        status = result.get("status")
        if status != "delivered":
            self.error_type = str(status)
        elif not (result.get("resp") or {}).get("ok", True):
            self.error_type = ERROR_RESPONSE


_UNTRACED = Call()


def client_call(key: str, peer_id: Optional[str]) -> Call:
    """Trace and time a request; ``peer_id`` ``None`` means a broadcast."""
    instruments = _instruments
    if instruments is None:
        return _UNTRACED
    attributes: Dict[str, Any] = {"rpc.system": RPC_SYSTEM, "rpc.method": key}
    if peer_id is not None:
        attributes["server.address"] = peer_id
    return Call.start(instruments, key, instruments.trace.SpanKind.CLIENT,
                      attributes, instruments.client_duration)


def publish_call(topic: str) -> Call:
    """Trace a publish so subscribers can link their handling to it."""
    instruments = _instruments
    if instruments is None:
        return _UNTRACED
    return Call.start(
        instruments, f"publish {topic}", instruments.trace.SpanKind.PRODUCER,
        {"messaging.system": RPC_SYSTEM, "messaging.destination.name": topic},
    )


def operation(name: str) -> Call:
    """Trace a unit of work spanning several calls, e.g. one CLI command."""
    instruments = _instruments
    if instruments is None:
        return _UNTRACED
    return Call.start(instruments, name, instruments.trace.SpanKind.INTERNAL, {})


def traced_handler(callback: Handler) -> Handler:
    """Wrap a ``serve`` callback in a server span and a duration metric."""

    def handle(payload: dict, ctx: dict) -> Optional[dict]:
        instruments = _instruments
        if instruments is None:
            return callback(payload, ctx)
        key = ctx.get("key", "")
        attributes: Dict[str, Any] = {"rpc.system": RPC_SYSTEM, "rpc.method": key}
        started = time.perf_counter()
        error_type: Optional[str] = None
        with instruments.tracer.start_as_current_span(
            key, context=instruments.incoming(ctx.get("traceparent"), ctx.get("tracestate")),
            kind=instruments.trace.SpanKind.SERVER,
            # client.address stays off the metric: CLI peer ids are unbounded
            attributes={**attributes, "client.address": ctx.get("source", "")},
        ) as span:
            try:
                result = callback(payload, ctx)
                if isinstance(result, dict) and not result.get("ok", True):
                    error_type = ERROR_RESPONSE
                return result
            except Exception as ex:
                error_type = type(ex).__qualname__
                raise
            finally:
                instruments.finish(span, error_type, attributes,
                                   instruments.server_duration, started)

    return handle


def traced_listener(topic: str, handler: Listener) -> Listener:
    """Wrap a topic handler in a consumer span linked to the publisher's."""

    def handle(msg: Any) -> None:
        instruments = _instruments
        if instruments is None:
            handler(msg)
            return
        env = msg.env
        with instruments.tracer.start_as_current_span(
            f"process {topic}",
            context=instruments.incoming(env.traceparent, env.tracestate),
            kind=instruments.trace.SpanKind.CONSUMER,
            attributes={"messaging.system": RPC_SYSTEM, "messaging.destination.name": topic},
        ):
            handler(msg)

    return handle


class TraceContextFormatter(logging.Formatter):
    """Append the active trace and span ids, so a log line links to its trace."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        instruments = _instruments
        if instruments is None:
            return line
        span_context = instruments.trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return line
        return (f"{line} trace_id={span_context.trace_id:032x}"
                f" span_id={span_context.span_id:016x}")
