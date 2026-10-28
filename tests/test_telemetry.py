"""Tests for libby's optional OpenTelemetry instrumentation.

The disabled cases need nothing beyond libby. The traced cases need the
``otel`` extra (tox installs it) and run per transport, recording into
in-memory exporters installed once for the whole module, since the OTel API
allows one global provider per process.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from libby import Client, KeywordError, telemetry
from libby.libby import Libby

from tests.test_client_integration import (GROUP_ID, RABBITMQ_URL, RPC_TIMEOUT_S,
                                           _broker_available, _free_endpoint,
                                           _RabbitFixtureDaemon, _ZmqFixtureDaemon)

# find_spec on the submodule raises when the opentelemetry package itself is absent
HAS_OTEL = (importlib.util.find_spec("opentelemetry") is not None
            and importlib.util.find_spec("opentelemetry.sdk") is not None)
PUBLISH_WAIT_S = 5.0
UNREACHABLE_TTL_MS = 300
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

# Every traced class is skipUnless-guarded on HAS_OTEL, which pylint cannot
# see through
# pylint: disable=possibly-used-before-assignment

if HAS_OTEL:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind

    SPANS = InMemorySpanExporter()
    METRICS = InMemoryMetricReader()


def setUpModule() -> None:  # pylint: disable=invalid-name
    """Install in-memory providers once; the OTel API refuses to replace them."""
    if not HAS_OTEL:
        return
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(SPANS))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))


class DisabledTests(unittest.TestCase):
    """With telemetry off, every hook passes straight through."""

    def setUp(self) -> None:  # pylint: disable=invalid-name
        telemetry.disable()

    def test_client_call_carries_no_trace_context(self):
        """Send requests untraced."""
        with telemetry.client_call("positionvalue", "hsfei.adc") as call:
            call.check_request({"status": "timeout"})
        self.assertIsNone(call.traceparent)
        self.assertIsNone(call.tracestate)
        self.assertIsNone(call.error_type)

    def test_handler_is_called_unchanged(self):
        """Hand payload and ctx to the callback and return its result as is."""
        seen: List[Any] = []

        def callback(payload: dict, ctx: dict) -> dict:
            seen.append((payload, ctx))
            return {"ok": True}

        ctx = {"key": "k", "traceparent": TRACEPARENT}
        self.assertEqual(telemetry.traced_handler(callback)({"a": 1}, ctx), {"ok": True})
        self.assertEqual(seen, [({"a": 1}, ctx)])

    def test_log_lines_are_unchanged(self):
        """Append nothing to a log line."""
        formatter = telemetry.TraceContextFormatter("%(message)s")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        self.assertEqual(formatter.format(record), "hello")

    def test_configure_needs_an_endpoint(self):
        """Stay off when no OTLP endpoint is set."""
        variables = {name: "" for name in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        )}
        with mock.patch.dict(os.environ, variables):
            self.assertIsNone(telemetry.configure("hsfei.adc"))
        self.assertFalse(telemetry.enabled())

    def test_sdk_disabled_wins_over_an_endpoint(self):
        """Honour OTEL_SDK_DISABLED even when an endpoint is set."""
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                                          "OTEL_SDK_DISABLED": "true"}):
            self.assertFalse(telemetry.export_requested())


def _by_kind(spans: List[Any], kind: Any) -> List[Any]:
    return [span for span in spans if span.kind == kind]


def _duration_points(name: str) -> List[Any]:
    data = METRICS.get_metrics_data()
    return [
        point
        for resource in (data.resource_metrics if data else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics if metric.name == name
        for point in metric.data.data_points
    ]


def _attributes(points: List[Any]) -> List[Dict[str, Any]]:
    return [dict(point.attributes) for point in points]


@unittest.skipUnless(HAS_OTEL, "needs the otel extra")
class TracedUnitTests(unittest.TestCase):
    """Span shape for calls that need no transport."""

    def setUp(self) -> None:  # pylint: disable=invalid-name
        SPANS.clear()
        telemetry.use_current_providers()

    def tearDown(self) -> None:  # pylint: disable=invalid-name
        telemetry.disable()

    def test_handler_continues_the_callers_trace(self):
        """Parent the server span on the traceparent from the envelope."""
        handler = telemetry.traced_handler(lambda payload, ctx: {"ok": True})
        handler({}, {"key": "positionvalue", "source": "cli",
                     "traceparent": TRACEPARENT, "tracestate": None})
        (span,) = SPANS.get_finished_spans()
        self.assertEqual(span.kind, SpanKind.SERVER)
        self.assertEqual(span.name, "positionvalue")
        self.assertEqual(f"{span.context.trace_id:032x}", TRACEPARENT.split("-")[1])
        self.assertEqual(f"{span.parent.span_id:016x}", TRACEPARENT.split("-")[2])

    def test_handler_exception_is_recorded_and_raised(self):
        """Mark the span failed with the exception type, and still raise."""

        def callback(_payload: dict, _ctx: dict) -> dict:
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            telemetry.traced_handler(callback)({}, {"key": "k"})
        (span,) = SPANS.get_finished_spans()
        self.assertEqual(span.attributes["error.type"], "ValueError")
        self.assertFalse(span.status.is_ok)

    def test_log_lines_inside_a_span_carry_its_ids(self):
        """Append trace_id and span_id while a span is active."""
        formatter = telemetry.TraceContextFormatter("%(message)s")
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        with telemetry.operation("work"):
            line = formatter.format(record)
        (span,) = SPANS.get_finished_spans()
        self.assertEqual(line, f"hello trace_id={span.context.trace_id:032x}"
                               f" span_id={span.context.span_id:016x}")

    def test_unanswered_request_is_a_timeout_error(self):
        """Record a request nobody answers as error.type timeout."""
        libby = Libby.zmq("telemetryprobe", _free_endpoint(),
                          {"nobody": _free_endpoint()})
        try:
            libby.request("nobody", "positionvalue", {}, ttl_ms=UNREACHABLE_TTL_MS)
        finally:
            libby.stop()
        (span,) = _by_kind(SPANS.get_finished_spans(), SpanKind.CLIENT)
        self.assertEqual(span.attributes["error.type"], "timeout")
        self.assertIn({"rpc.system": "libby", "rpc.method": "positionvalue",
                       "server.address": "nobody", "error.type": "timeout"},
                      _attributes(_duration_points("rpc.client.call.duration")))


class _Bases:  # pylint: disable=too-few-public-methods
    """Namespace for the transport-agnostic traced cases."""

    @unittest.skipUnless(HAS_OTEL, "needs the otel extra")
    class TracedCases(unittest.TestCase):
        """Tracing that must hold identically on every transport.

        Concrete subclasses supply ``client`` and ``peer`` from ``setUpClass``.
        """

        client: Client
        peer: str

        def setUp(self) -> None:  # pylint: disable=invalid-name
            telemetry.use_current_providers()
            SPANS.clear()

        def tearDown(self) -> None:  # pylint: disable=invalid-name
            telemetry.disable()

        def test_request_and_handler_share_one_trace(self):
            """Parent the daemon's server span on the client's request span."""
            self.client.get(f"{self.peer}.positionvalue", timeout_s=RPC_TIMEOUT_S)
            spans = SPANS.get_finished_spans()
            (client_span,) = _by_kind(spans, SpanKind.CLIENT)
            (server_span,) = _by_kind(spans, SpanKind.SERVER)
            self.assertEqual(client_span.name, "positionvalue")
            self.assertEqual(client_span.attributes["server.address"], self.peer)
            self.assertEqual(server_span.context.trace_id, client_span.context.trace_id)
            self.assertEqual(server_span.parent.span_id, client_span.context.span_id)
            self.assertIn({"rpc.system": "libby", "rpc.method": "positionvalue"},
                          _attributes(_duration_points("rpc.server.call.duration")))

        def test_error_response_marks_both_spans(self):
            """Record a refused write as error_response on client and server."""
            with self.assertRaises(KeywordError):
                self.client.set(f"{self.peer}.isreferenced", False, timeout_s=RPC_TIMEOUT_S)
            for span in SPANS.get_finished_spans():
                self.assertEqual(span.attributes["error.type"], telemetry.ERROR_RESPONSE)


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class RabbitMQTracedTests(_Bases.TracedCases):
    """Traced cases over RabbitMQ."""

    @classmethod
    def setUpClass(cls):
        cls.daemon = _RabbitFixtureDaemon()
        cls.daemon.peer_id = "telemetrytestrmq"
        cls.daemon.start()
        cls.peer = f"{GROUP_ID}.telemetrytestrmq"
        cls.client = Client.rabbitmq(rabbitmq_url=RABBITMQ_URL)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.daemon.stop()


class ZmqTracedTests(_Bases.TracedCases):
    """Traced cases over ZMQ."""

    @classmethod
    def setUpClass(cls):
        endpoint = _free_endpoint()
        cls.daemon = _ZmqFixtureDaemon()
        cls.daemon.peer_id = "telemetrytestzmq"
        cls.daemon.bind = endpoint
        cls.daemon.start()
        cls.peer = f"{GROUP_ID}.telemetrytestzmq"
        cls.client = Client.zmq(bind=_free_endpoint(), address_book={cls.peer: endpoint})

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.daemon.stop()


@unittest.skipUnless(HAS_OTEL, "needs the otel extra")
class ZmqPublishTests(unittest.TestCase):
    """A subscriber's handling joins the publisher's trace."""

    def setUp(self) -> None:  # pylint: disable=invalid-name
        telemetry.use_current_providers()
        SPANS.clear()

    def tearDown(self) -> None:  # pylint: disable=invalid-name
        telemetry.disable()

    def test_consumer_span_follows_the_publish(self):
        """Parent the subscriber's process span on the publish span."""
        publisher_bind, subscriber_bind = _free_endpoint(), _free_endpoint()
        publisher = Libby.zmq("telemetrypub", publisher_bind, {"telemetrysub": subscriber_bind})
        subscriber = Libby.zmq("telemetrysub", subscriber_bind, {"telemetrypub": publisher_bind})
        received = threading.Event()
        try:
            subscriber.listen("telemetry.topic", lambda _msg: received.set())
            publisher.publish("telemetry.topic", {"n": 1})
            self.assertTrue(received.wait(PUBLISH_WAIT_S))
        finally:
            publisher.stop()
            subscriber.stop()
        spans = SPANS.get_finished_spans()
        (producer,) = _by_kind(spans, SpanKind.PRODUCER)
        consumer: Optional[Any] = next(iter(_by_kind(spans, SpanKind.CONSUMER)), None)
        self.assertIsNotNone(consumer)
        self.assertEqual(consumer.parent.span_id, producer.context.span_id)


if __name__ == "__main__":
    unittest.main()
