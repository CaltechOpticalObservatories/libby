"""Unit tests for the InfluxDB sink's sample mapping and error contract.

Needs the optional dependency (``pip install libby[influxdb]``); tox installs
it, so these run in CI rather than skipping.
"""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from typing import Any, List, Optional

from libby.keygrabber import Sample, SinkWriteError

HAS_INFLUX = importlib.util.find_spec("influxdb_client") is not None

# Every class below is skipUnless-guarded on HAS_INFLUX, which pylint
# cannot see through
# pylint: disable=possibly-used-before-assignment

if HAS_INFLUX:
    from libby.keygrabber.influx import (NO_UNITS, InfluxConfig, InfluxSink,
                                         field_value, to_point)


def _sample(
    keyword: str = "positionvalue",
    value: Any = 7.5,
    units: Optional[str] = "mm",
) -> Sample:
    return Sample(keyword=keyword, group="hsfei", peer="adc", value=value,
                  units=units,
                  timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))


class _FakeWriteApi:  # pylint: disable=too-few-public-methods
    """Records write calls, or raises to exercise the error contract."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.records: List[Any] = []
        self._error = error

    def write(self, bucket: str, org: str, record: Any) -> None:
        """Record the batch, or raise the configured error."""
        if self._error is not None:
            raise self._error
        self.records.append((bucket, org, record))


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class FieldValueTests(unittest.TestCase):
    """Coercion of keyword values to Influx field types."""

    def test_int_becomes_float(self):
        """Avoid an int/float type conflict between peers for one keyword."""
        coerced = field_value(12)
        self.assertIsInstance(coerced, float)
        self.assertEqual(coerced, 12.0)

    def test_bool_stays_bool(self):
        """Keep bools as bools, despite bool being a subclass of int."""
        self.assertIs(field_value(True), True)
        self.assertIsInstance(field_value(False), bool)

    def test_float_stays_float(self):
        """Pass a float through unchanged."""
        self.assertEqual(field_value(7.5), 7.5)

    def test_string_stays_string(self):
        """Store a string keyword as a string field."""
        self.assertEqual(field_value("Ready"), "Ready")


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class ToPointTests(unittest.TestCase):
    """Mapping a sample onto the measurement-per-keyword schema."""

    def test_measurement_is_the_keyword_name(self):
        """Use the keyword name as the measurement, with group/peer as tags."""
        line = to_point(_sample()).to_line_protocol()
        self.assertTrue(line.startswith("positionvalue,"))
        self.assertIn("group=hsfei", line)
        self.assertIn("peer=adc", line)
        self.assertIn("units=mm", line)
        self.assertIn("value=7.5", line)

    def test_null_value_is_skipped(self):
        """Skip a null rather than raising: lasterror is null most of the time."""
        self.assertIsNone(to_point(_sample(keyword="lasterror", value=None)))

    def test_missing_units_get_a_placeholder(self):
        """Keep a unitless keyword on one series instead of dropping the tag."""
        line = to_point(_sample(units=None)).to_line_protocol()
        self.assertIn(f"units={NO_UNITS}", line)

    def test_timestamp_is_carried_at_nanosecond_precision(self):
        """Stamp the point with the sample's own read time."""
        line = to_point(_sample()).to_line_protocol()
        expected_ns = int(_sample().timestamp.timestamp() * 1_000_000_000)
        self.assertTrue(line.endswith(str(expected_ns)))

    def test_string_value_is_quoted_by_the_client(self):
        """Let the client escape a string field rather than hand-rolling it."""
        line = to_point(_sample(keyword="lasterror",
                                value='failed: "x", y', units=None)).to_line_protocol()
        self.assertIn('value="failed: \\"x\\", y"', line)


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class InfluxSinkWriteTests(unittest.TestCase):
    """The sink's write contract, without a server."""

    # The write api is injected in place of connect() so these need no server
    # pylint: disable=protected-access

    def setUp(self) -> None:
        self.sink = InfluxSink(InfluxConfig(
            url="http://localhost:8086", org="hispec",
            bucket="telemetry", token="unused"))

    def test_writes_one_request_for_the_whole_batch(self):
        """Send a batch as a single request and report the count written."""
        api = _FakeWriteApi()
        self.sink._write_api = api
        written = self.sink.write([_sample(), _sample(keyword="isconnected",
                                                      value=True, units=None)])
        self.assertEqual(written, 2)
        self.assertEqual(len(api.records), 1)
        self.assertEqual(len(api.records[0][2]), 2)

    def test_nulls_are_not_counted_as_written(self):
        """Report only the samples that reached the backend."""
        api = _FakeWriteApi()
        self.sink._write_api = api
        written = self.sink.write([_sample(),
                                   _sample(keyword="lasterror", value=None)])
        self.assertEqual(written, 1)

    def test_all_null_batch_makes_no_request(self):
        """Skip the request entirely when nothing is storable."""
        api = _FakeWriteApi()
        self.sink._write_api = api
        self.assertEqual(self.sink.write([_sample(value=None)]), 0)
        self.assertEqual(api.records, [])

    def test_backend_failure_becomes_a_sink_write_error(self):
        """Translate the client's own exceptions into one retryable error."""
        self.sink._write_api = _FakeWriteApi(error=OSError("connection refused"))
        with self.assertRaises(SinkWriteError):
            self.sink.write([_sample()])

    def test_writing_before_connect_is_a_sink_write_error(self):
        """Fail a write on an unconnected sink the same retryable way."""
        with self.assertRaises(SinkWriteError):
            self.sink.write([_sample()])

    def test_is_connected_is_false_before_connect(self):
        """Report not connected rather than raising."""
        self.assertFalse(self.sink.is_connected())

    def test_close_without_connect_is_safe(self):
        """Allow teardown of a sink that never opened."""
        self.sink.close()


if __name__ == "__main__":
    unittest.main()
