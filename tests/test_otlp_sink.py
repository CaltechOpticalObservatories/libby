"""Tests for the keygrabber's OTLP sink: sample mapping, export, and config.

Needs the optional ``otel`` extra; tox installs it. The end-to-end case also
needs a Collector feeding a Prometheus, and runs only when
``LIBBY_TEST_PROMETHEUS_URL`` names that Prometheus and
``OTEL_EXPORTER_OTLP_ENDPOINT`` the Collector.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from unittest import mock

from libby.config import ConfigError
from libby.keygrabber import Sample, SinkWriteError
from libby.keygrabber.config import build_sink

# find_spec on the submodule raises when the opentelemetry package itself is absent
HAS_OTEL = (importlib.util.find_spec("opentelemetry") is not None
            and importlib.util.find_spec("opentelemetry.sdk") is not None)
PROMETHEUS_URL = os.environ.get("LIBBY_TEST_PROMETHEUS_URL")
ARRIVAL_TIMEOUT_S = 30.0
READ_AT = datetime(2026, 11, 4, 12, 0, 0, 123456, tzinfo=timezone.utc)

# Every class below is skipUnless-guarded on HAS_OTEL, which pylint cannot
# see through
# pylint: disable=possibly-used-before-assignment

if HAS_OTEL:
    # pylint: disable=ungrouped-imports
    from opentelemetry.sdk.metrics.export import MetricExportResult

    from libby.keygrabber.otlp import METRIC_NAME, OtlpSink, to_point


def _sample(keyword: str = "positionvalue", value: Any = 7.5,
            units: Optional[str] = "mm", timestamp: datetime = READ_AT) -> Sample:
    return Sample(keyword=keyword, group="hsfei", peer="adc", value=value,
                  units=units, timestamp=timestamp)


class _RecordingExporter:
    """Stands in for OTLPMetricExporter, keeping what it was asked to export."""

    def __init__(self, result: Any = None, error: Optional[Exception] = None) -> None:
        self.exports: List[Any] = []
        self._result = result
        self._error = error

    def export(self, data: Any, timeout_millis: float = 0) -> Any:
        """Record one export and answer as configured."""
        del timeout_millis
        if self._error is not None:
            raise self._error
        self.exports.append(data)
        return self._result if self._result is not None else MetricExportResult.SUCCESS

    def shutdown(self) -> None:
        """Nothing to release."""


@unittest.skipUnless(HAS_OTEL, "needs the otel extra")
class ToPointTests(unittest.TestCase):
    """Mapping one sample to one gauge point."""

    def test_a_float_keeps_its_labels_and_read_time(self):
        """Label by group, peer, keyword and units, at the read time to the microsecond."""
        point = to_point(_sample())
        self.assertEqual(point.value, 7.5)
        self.assertEqual(dict(point.attributes), {
            "group": "hsfei", "peer": "adc", "keyword": "positionvalue", "units": "mm"})
        self.assertEqual(point.time_unix_nano,
                         int(READ_AT.timestamp()) * 1_000_000_000 + 123_456_000)

    def test_a_bool_becomes_zero_or_one(self):
        """Store a flag as a number, as Prometheus can only hold numbers."""
        self.assertEqual(to_point(_sample(value=True)).value, 1.0)
        self.assertEqual(to_point(_sample(value=False)).value, 0.0)

    def test_no_units_means_no_units_label(self):
        """Leave the label off rather than storing an empty one."""
        self.assertNotIn("units", to_point(_sample(units=None)).attributes)

    def test_strings_and_nulls_are_skipped(self):
        """Skip what a gauge cannot hold."""
        self.assertIsNone(to_point(_sample(value="moving")))
        self.assertIsNone(to_point(_sample(value=None)))


@unittest.skipUnless(HAS_OTEL, "needs the otel extra")
class WriteTests(unittest.TestCase):
    """Exporting a batch."""

    def test_a_batch_is_one_gauge_with_a_point_per_number(self):
        """Export one libby_keyword metric and report the numbers stored."""
        exporter = _RecordingExporter()
        sink = OtlpSink(exporter=exporter)
        written = sink.write([_sample(), _sample(keyword="status", value="ok"),
                              _sample(keyword="ismoving", value=False)])
        self.assertEqual(written, 2)
        self.assertEqual(len(exporter.exports), 1)
        metric = exporter.exports[0].resource_metrics[0].scope_metrics[0].metrics[0]
        self.assertEqual(metric.name, METRIC_NAME)
        self.assertEqual(len(metric.data.data_points), 2)

    def test_the_resource_has_no_random_instance_id(self):
        """Keep series continuous across restarts; an instance id would be a label."""
        exporter = _RecordingExporter()
        OtlpSink(exporter=exporter).write([_sample()])
        attributes = exporter.exports[0].resource_metrics[0].resource.attributes
        self.assertNotIn("service.instance.id", attributes)
        self.assertEqual(attributes["service.name"], "libby-keygrabber")

    def test_a_batch_with_no_numbers_exports_nothing(self):
        """Do not send an empty request."""
        exporter = _RecordingExporter()
        self.assertEqual(OtlpSink(exporter=exporter).write([_sample(value="ok")]), 0)
        self.assertEqual(exporter.exports, [])

    def test_a_rejected_export_is_retryable(self):
        """Raise the error RetryingWriter retries on."""
        sink = OtlpSink(exporter=_RecordingExporter(result=MetricExportResult.FAILURE))
        with self.assertRaises(SinkWriteError):
            sink.write([_sample()])

    def test_a_raising_exporter_is_retryable(self):
        """Turn a transport error into the same retryable error."""
        sink = OtlpSink(exporter=_RecordingExporter(error=ConnectionError("refused")))
        with self.assertRaisesRegex(SinkWriteError, "refused"):
            sink.write([_sample()])


@unittest.skipUnless(HAS_OTEL, "needs the otel extra")
class BuildTests(unittest.TestCase):
    """Building an otlp sink from its config."""

    def test_an_endpoint_is_required(self):
        """Refuse to start a sink that would export nowhere."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "OTEL_EXPORTER_OTLP_ENDPOINT"):
                build_sink("prometheus", {"type": "otlp"})

    def test_the_endpoint_from_otel_env_is_enough(self):
        """Build from the variable otel.env already sets."""
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://b:4318"},
                             clear=True):
            self.assertIsInstance(build_sink("prometheus", {"type": "otlp"}), OtlpSink)

    def test_an_unknown_key_is_named(self):
        """Refuse a misspelt timeout rather than exporting with the default."""
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://b:4318"},
                             clear=True):
            with self.assertRaisesRegex(ConfigError, "unknown key.*timout_s"):
                build_sink("prometheus", {"type": "otlp", "timout_s": 30})

    def test_a_bad_timeout_names_the_sink(self):
        """Say which sink's timeout is wrong."""
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://b:4318"},
                             clear=True):
            with self.assertRaisesRegex(ConfigError, "sinks.prometheus.timeout_s"):
                build_sink("prometheus", {"type": "otlp", "timeout_s": 0})


def _query(expr: str) -> List[Any]:
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)["data"]["result"]


@unittest.skipUnless(HAS_OTEL and PROMETHEUS_URL, "needs LIBBY_TEST_PROMETHEUS_URL")
class PrometheusTests(unittest.TestCase):
    """A real export through a Collector into Prometheus."""

    def test_samples_arrive_as_libby_keyword_series(self):
        """Query back a fresh sample, and one read a while ago."""
        keyword = f"otlptest{uuid.uuid4().hex[:8]}"
        late = datetime.now(timezone.utc) - timedelta(minutes=30)
        sink = build_sink("prometheus", {"type": "otlp"})
        sink.connect()
        try:
            sink.write([_sample(keyword=keyword, value=1.25,
                                timestamp=datetime.now(timezone.utc))])
            sink.write([_sample(keyword=f"{keyword}late", value=2.5, timestamp=late)])
        finally:
            sink.close()
        deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
        while time.monotonic() < deadline:
            fresh = _query(f'{METRIC_NAME}{{keyword="{keyword}"}}')
            late_points = _query(f'last_over_time({METRIC_NAME}'
                                 f'{{keyword="{keyword}late"}}[1h])')
            if fresh and late_points:
                break
            time.sleep(1)
        self.assertEqual(fresh[0]["value"][1], "1.25")
        self.assertEqual(fresh[0]["metric"]["units"], "mm")
        self.assertEqual(late_points[0]["value"][1], "2.5")


if __name__ == "__main__":
    unittest.main()
