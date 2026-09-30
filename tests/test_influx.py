"""Unit tests for the InfluxDB sink and source: sample mapping, queries, errors.

Needs the optional dependency (``pip install libby[influxdb]``); tox installs
it, so these run in CI rather than skipping.
"""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from libby.keygrabber import Sample, SinkError, SinkWriteError

HAS_INFLUX = importlib.util.find_spec("influxdb_client") is not None

# Every class below is skipUnless-guarded on HAS_INFLUX, which pylint
# cannot see through
# pylint: disable=possibly-used-before-assignment

if HAS_INFLUX:
    from influxdb_client.client.flux_table import FluxRecord

    # Grouped under the guard with the client import, not with libby's above
    # pylint: disable-next=ungrouped-imports
    from libby.keygrabber.influx import (NO_UNITS, InfluxConfig, InfluxSink,
                                         InfluxSource, field_value,
                                         flux_string, flux_time, to_point,
                                         to_sample)


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


def _record(
    keyword: str = "positionvalue",
    value: Any = 7.5,
    units: Optional[str] = "mm",
    peer: str = "adc",
    minute: int = 0,
) -> "FluxRecord":
    return FluxRecord(0, {
        "_measurement": keyword, "_field": "value", "_value": value,
        "_time": datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc),
        "group": "hsfei", "peer": peer, "units": units,
    })


class _FakeTable:  # pylint: disable=too-few-public-methods
    """Stands in for a FluxTable, which the source only reads records from."""

    def __init__(self, records: List[Any]) -> None:
        self.records = records


class _FakeQueryApi:  # pylint: disable=too-few-public-methods
    """Records query calls and returns canned tables, or raises."""

    def __init__(self, tables: Optional[List[_FakeTable]] = None,
                 error: Optional[Exception] = None) -> None:
        self.queries: List[Any] = []
        self._tables = tables or []
        self._error = error

    def query(self, query: str, org: str) -> List[_FakeTable]:
        """Record the query, then return the tables or raise the error."""
        self.queries.append((query, org))
        if self._error is not None:
            raise self._error
        return self._tables


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


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class FluxLiteralTests(unittest.TestCase):
    """Quoting values into Flux without parameterized queries."""

    def test_plain_string_is_quoted(self):
        """Wrap an ordinary name in double quotes."""
        self.assertEqual(flux_string("adc"), '"adc"')

    def test_quote_and_backslash_are_escaped(self):
        """Keep a quote or backslash from ending the literal early."""
        self.assertEqual(flux_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_dollar_is_escaped(self):
        """Stop Flux reading ${ as string interpolation."""
        self.assertEqual(flux_string("${x}"), '"\\${x}"')

    def test_time_is_converted_to_utc(self):
        """Format an offset-aware time as a UTC RFC3339 literal."""
        pacific = timezone(timedelta(hours=-7))
        moment = datetime(2026, 1, 1, 5, 0, tzinfo=pacific)
        self.assertEqual(flux_time(moment), "2026-01-01T12:00:00.000000Z")


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class ToSampleTests(unittest.TestCase):
    """Mapping a queried record back onto a sample."""

    def test_record_round_trips_to_a_sample(self):
        """Recover the sample that to_point stored."""
        self.assertEqual(to_sample(_record()), _sample())

    def test_units_placeholder_becomes_none(self):
        """Undo the placeholder that keeps unitless keywords on one series."""
        self.assertIsNone(to_sample(_record(units=NO_UNITS)).units)


@unittest.skipUnless(HAS_INFLUX, "influxdb-client not installed")
class InfluxSourceReadTests(unittest.TestCase):
    """The source's read contract, without a server."""

    # The query api is injected in place of connect() so these need no server
    # pylint: disable=protected-access

    START = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    STOP = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.source = InfluxSource(InfluxConfig(
            url="http://localhost:8086", org="hispec",
            bucket="telemetry", token="unused"))

    def _query(self, **kwargs: Any) -> str:
        api = _FakeQueryApi()
        self.source._query_api = api
        self.source.read("positionvalue", self.START, **kwargs)
        self.assertEqual(api.queries[0][1], "hispec")
        return api.queries[0][0]

    def test_query_selects_bucket_measurement_and_field(self):
        """Query the configured bucket for the keyword's value field."""
        query = self._query()
        self.assertIn('from(bucket: "telemetry")', query)
        self.assertIn('r._measurement == "positionvalue"', query)
        self.assertIn('r._field == "value"', query)
        self.assertNotIn("r.peer", query)

    def test_open_ended_range_has_no_stop(self):
        """Leave stop out so the range runs up to now."""
        query = self._query()
        self.assertIn("range(start: 2026-01-01T11:00:00.000000Z)", query)

    def test_stop_bounds_the_range(self):
        """Pass an explicit stop through to the range."""
        query = self._query(stop=self.STOP)
        self.assertIn("stop: 2026-01-01T13:00:00.000000Z", query)

    def test_peer_filter_is_escaped(self):
        """Filter on peer, quoting it so it cannot alter the query."""
        query = self._query(peer='adc" or true')
        self.assertIn('r.peer == "adc\\" or true"', query)

    def test_records_across_tables_come_back_oldest_first(self):
        """Merge every table's records, sorted by time."""
        self.source._query_api = _FakeQueryApi(tables=[
            _FakeTable([_record(peer="adc", minute=30)]),
            _FakeTable([_record(peer="fei", minute=10),
                        _record(peer="fei", minute=20)]),
        ])
        samples = self.source.read("positionvalue", self.START)
        self.assertEqual([(s.peer, s.timestamp.minute) for s in samples],
                         [("fei", 10), ("fei", 20), ("adc", 30)])

    def test_no_data_is_an_empty_list(self):
        """Return nothing rather than raising when the range is empty."""
        self.source._query_api = _FakeQueryApi()
        self.assertEqual(self.source.read("positionvalue", self.START), [])

    def test_backend_failure_becomes_a_sink_error(self):
        """Translate the client's own exceptions into one error type."""
        self.source._query_api = _FakeQueryApi(error=OSError("refused"))
        with self.assertRaises(SinkError):
            self.source.read("positionvalue", self.START)

    def test_reading_before_connect_is_a_sink_error(self):
        """Fail a read on an unconnected source rather than on None."""
        with self.assertRaises(SinkError):
            self.source.read("positionvalue", self.START)

    def test_is_connected_is_false_before_connect(self):
        """Report not connected rather than raising."""
        self.assertFalse(self.source.is_connected())

    def test_close_without_connect_is_safe(self):
        """Allow teardown of a source that never opened."""
        self.source.close()


if __name__ == "__main__":
    unittest.main()
