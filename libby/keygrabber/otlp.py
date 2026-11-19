"""OTLP sink: keyword samples as gauges, for Prometheus behind an OpenTelemetry Collector.

Every numeric and bool keyword is one gauge, ``libby_keyword``, labelled by
``group``, ``peer``, ``keyword`` and ``units``, so a PromQL query is one
metric name plus label matchers. Each point carries its keyword's read time
rather than an export time. String and null values are not stored.

Exports to the endpoint named by the standard ``OTEL_EXPORTER_OTLP_*``
variables. Needs the optional dependency: ``pip install libby[otel]``.
"""
from __future__ import annotations

import socket
from typing import Any, List, Optional, Sequence

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics.export import (Gauge, Metric, MetricExportResult, MetricsData,
                                              NumberDataPoint, ResourceMetrics, ScopeMetrics)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from .sink import Sample, SinkWriteError

METRIC_NAME = "libby_keyword"
SERVICE_NAME = "libby-keygrabber"
DEFAULT_TIMEOUT_S = 10.0
_SCOPE = InstrumentationScope("libby.keygrabber")
_NANOS_PER_SECOND = 1_000_000_000


def to_point(sample: Sample) -> Optional[NumberDataPoint]:
    """Map a sample to a gauge point, or ``None`` if it is not a number.

    A bool becomes 0 or 1, and every number a float, matching what Prometheus
    stores. Units are a label only when the keyword declares them.
    """
    value = sample.value
    if value is None or isinstance(value, str):
        return None
    attributes = {"group": sample.group, "peer": sample.peer, "keyword": sample.keyword}
    if sample.units:
        attributes["units"] = sample.units
    stamp = sample.timestamp
    nanos = (int(stamp.timestamp()) * _NANOS_PER_SECOND
             + stamp.microsecond * 1_000)
    return NumberDataPoint(attributes=attributes, start_time_unix_nano=0,
                           time_unix_nano=nanos, value=float(value))


class OtlpSink:
    """Exports samples as OTLP gauges, one request per batch."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S,
                 exporter: Optional[Any] = None) -> None:
        self._timeout_s = timeout_s
        self._exporter = exporter
        self._owns_exporter = exporter is None
        # Built by hand: Resource.create would add a random service.instance.id,
        # which Prometheus labels, starting new series on every restart
        self._resource = Resource({"service.name": SERVICE_NAME,
                                   "host.name": socket.gethostname()})

    def connect(self) -> None:
        """Create the exporter, reading the endpoint from the environment."""
        if self._owns_exporter:
            self.close()
            self._exporter = OTLPMetricExporter(timeout=self._timeout_s)

    def is_connected(self) -> bool:
        """Return whether an exporter exists; OTLP has nothing to ping."""
        return self._exporter is not None

    def write(self, samples: Sequence[Sample]) -> int:
        """Export a batch as one request, skipping samples that are not numbers."""
        if self._exporter is None:
            raise SinkWriteError("otlp sink is not connected")
        points: List[NumberDataPoint] = [
            point for point in map(to_point, samples) if point is not None]
        if not points:
            return 0
        data = MetricsData(resource_metrics=[ResourceMetrics(
            resource=self._resource, schema_url="",
            scope_metrics=[ScopeMetrics(scope=_SCOPE, schema_url="", metrics=[Metric(
                name=METRIC_NAME, description="libby keyword value",
                unit=None, data=Gauge(data_points=points))])],
        )])
        try:
            result = self._exporter.export(data, timeout_millis=self._timeout_s * 1000)
        # The exporter surfaces HTTP and socket errors with no common base, and
        # the caller's contract is a single retryable error
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise SinkWriteError(f"otlp export failed: {exc}") from exc
        if result is not MetricExportResult.SUCCESS:
            raise SinkWriteError("otlp export was rejected or did not arrive")
        return len(points)

    def close(self) -> None:
        """Shut the exporter down; safe to call when never connected."""
        exporter = self._exporter
        if self._owns_exporter:
            self._exporter = None
        if exporter is not None and self._owns_exporter:
            exporter.shutdown()
