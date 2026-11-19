"""Tests for the keyheader daemon, with a fake source in place of InfluxDB.

The unit cases call the daemon directly. The end-to-end case starts it on ZMQ
and requests a segment over the wire, as a camera daemon would.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import yaml

from libby import Client
from libby.config import ConfigError
from libby.keygrabber import Sample
from libby.keyheader import SERVICE_KEY, KeyheaderDaemon, parse_config, parse_time
from libby.keyheader.source import SourceReadError

T0 = datetime(2026, 11, 11, 8, 15, 0, tzinfo=timezone.utc)

SEGMENTS = [
    {"name": "EXP-BEGIN", "date_keyword": "DATE-BEG",
     "cards": [
         {"fits": "ADCPOS", "keyword": "hsfei.adc.positionvalue",
          "type": "float", "format": ".2f", "comment": "ADC position",
          "mode": "interpolate"},
         {"fits": "ADCSTAT", "keyword": "hsfei.adc.status", "type": "str"},
         {"fits": "PRESSURE", "keyword": "hsfei.press.value", "type": "float",
          "comment": "Dewar pressure"},
     ]},
]


def _free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{probe.getsockname()[1]}"


class _FakeSource:
    """Serves canned samples per (peer, keyword), and can be told to fail."""

    def __init__(self, samples: Dict[Tuple[str, str], List[Sample]]) -> None:
        self.samples = samples
        self.failing: set = set()
        self.reads: List[Tuple[str, datetime, Optional[datetime]]] = []
        self._lock = threading.Lock()

    def connect(self) -> None:
        """Nothing to open."""

    def is_connected(self) -> bool:
        """Always reachable."""
        return True

    def read(self, keyword, start, stop=None, group=None, peer=None):
        """Return the canned samples inside the range."""
        with self._lock:
            self.reads.append((keyword, start, stop))
        if keyword in self.failing:
            raise SourceReadError("backend unreachable")
        return [s for s in self.samples.get((peer, keyword), [])
                if s.group == group and start <= s.timestamp
                and (stop is None or s.timestamp < stop)]

    def close(self) -> None:
        """Nothing to release."""


def _sample(peer: str, keyword: str, seconds: float, value) -> Sample:
    return Sample(keyword=keyword, group="hsfei", peer=peer, value=value,
                  units=None, timestamp=T0 + timedelta(seconds=seconds))


def _source() -> _FakeSource:
    return _FakeSource({
        ("adc", "positionvalue"): [_sample("adc", "positionvalue", -5, 1.0),
                                   _sample("adc", "positionvalue", 5, 2.0)],
        ("adc", "status"): [_sample("adc", "status", -3, "Ready")],
    })


class _TestKeyheader(KeyheaderDaemon):
    """Keyheader reading a fake source instead of InfluxDB."""

    source: _FakeSource

    def make_source(self):
        """Return the injected source."""
        return self.source


class SegmentTests(unittest.TestCase):
    """The daemon builds a segment's entries from the source."""

    def setUp(self):
        self.source = _source()
        self.daemon = KeyheaderDaemon()
        self.daemon.configure(parse_config({"segments": SEGMENTS}), self.source)

    def tearDown(self):
        self.daemon.on_stop()

    def test_segment_entries(self):
        """Interpolate, take nearest, and blank what is missing."""
        entries = self.daemon.segment("EXP-BEGIN", T0)
        self.assertEqual([e["keyword"] for e in entries],
                         ["COMMENT", "DATE-BEG", "ADCPOS", "ADCSTAT", "PRESSURE"])
        by_name = {e["keyword"]: e for e in entries}
        self.assertEqual(by_name["ADCPOS"]["value"], 1.5)
        self.assertEqual(by_name["ADCSTAT"]["value"], "Ready")
        self.assertIsNone(by_name["PRESSURE"]["value"])
        self.assertEqual(by_name["PRESSURE"]["comment"],
                         "no sample: Dewar pressure")
        self.assertEqual(self.daemon.counters.blank_cards, 1)

    def test_read_failure_blanks_one_card(self):
        """Blank only the card whose read failed, not the whole request."""
        self.source.failing.add("status")
        by_name = {e["keyword"]: e for e in self.daemon.segment("EXP-BEGIN", T0)}
        self.assertEqual(by_name["ADCSTAT"]["comment"], "read failed")
        self.assertEqual(by_name["ADCPOS"]["value"], 1.5)
        self.assertEqual(self.daemon.counters.read_errors, 1)

    def test_unknown_segment(self):
        """Name the segments that do exist."""
        with self.assertRaisesRegex(ValueError, "EXP-BEGIN"):
            self.daemon.segment("EXP-END", T0)

    def test_request_payload(self):
        """Parse the request and reply with the entries and the moment."""
        # pylint: disable-next=protected-access
        reply = self.daemon._serve({"segment": "EXP-BEGIN",
                                    "time": "2026-11-11T08:15:00Z"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["time"], "2026-11-11T08:15:00.000")
        self.assertEqual(len(reply["cards"]), 5)

    def test_request_needs_segment_and_time(self):
        """Reject a request missing either field."""
        # pylint: disable=protected-access
        with self.assertRaisesRegex(ValueError, "segment"):
            self.daemon._serve({"time": "2026-11-11T08:15:00Z"})
        with self.assertRaisesRegex(ValueError, "time"):
            self.daemon._serve({"segment": "EXP-BEGIN"})


class ParseTimeTests(unittest.TestCase):
    """Request times are ISO 8601, defaulting to UTC."""

    def test_forms(self):
        """Accept Z, an offset, or no zone at all."""
        for text in ("2026-11-11T08:15:00Z", "2026-11-11T08:15:00+00:00",
                     "2026-11-11T08:15:00", "2026-11-11T00:15:00-08:00"):
            with self.subTest(text=text):
                self.assertEqual(parse_time(text), T0)

    def test_rejects_garbage(self):
        """Fail clearly on a time that is not ISO 8601."""
        with self.assertRaisesRegex(ValueError, "ISO 8601"):
            parse_time("yesterday")


class ReloadTests(unittest.TestCase):
    """reload swaps segments, and refuses source changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.path = os.path.join(self.tmp.name, "keyheader.yaml")
        self._write(SEGMENTS)
        self.daemon = _TestKeyheader.from_config_file(self.path)
        self.daemon.configure(parse_config(self.daemon._config), _source())  # pylint: disable=protected-access

    def tearDown(self):
        self.daemon.on_stop()
        self.tmp.cleanup()

    def _write(self, segments, source=None) -> None:
        config = {"peer_id": "keyheader", "group_id": "hispec",
                  "source": source or {"type": "influxdb"},
                  "segments": segments}
        with open(self.path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle)

    def test_reload_adds_a_segment(self):
        """Pick up a new segment without a restart."""
        extra = {"name": "EXP-END", "cards": [
            {"fits": "ADCPOSE", "keyword": "hsfei.adc.positionvalue",
             "type": "float", "mode": "prior"}]}
        self._write(SEGMENTS + [extra])
        self.daemon._reload()  # pylint: disable=protected-access
        entries = self.daemon.segment("EXP-END", T0)
        self.assertEqual(entries[-1]["value"], 1.0)

    def test_reload_refuses_source_change(self):
        """Refuse a change that needs a new connection."""
        self._write(SEGMENTS, source={"type": "influxdb", "url": "elsewhere"})
        with self.assertRaisesRegex(ConfigError, "restart"):
            self.daemon._reload()  # pylint: disable=protected-access

    def test_bad_file_leaves_segments_alone(self):
        """Keep serving the old segments when the new file is invalid."""
        self._write([{"name": "BAD", "cards": [
            {"fits": "SIMPLE", "keyword": "a.b.c", "type": "float"}]}])
        with self.assertRaises(ConfigError):
            self.daemon._reload()  # pylint: disable=protected-access
        self.daemon.segment("EXP-BEGIN", T0)


class ZmqEndToEndTests(unittest.TestCase):
    """A client requests a segment over the wire."""

    def setUp(self):
        self.endpoint = _free_endpoint()
        self.daemon = _TestKeyheader.from_config({
            "peer_id": "keyheaderzmq",
            "group_id": "hispec",
            "transport": "zmq",
            "bind": self.endpoint,
            "address_book": {},
            "discovery_enabled": False,
            "segments": SEGMENTS,
        })
        self.daemon.source = _source()
        self.daemon.start()

    def tearDown(self):
        self.daemon.stop()

    def test_request_segment(self):
        """Answer the service with entries, and serve control keywords."""
        peer = "hispec.keyheaderzmq"
        with Client.zmq(bind=_free_endpoint(),
                        address_book={peer: self.endpoint}) as client:
            # pylint: disable-next=protected-access
            envelope = client._libby.rpc(
                peer, SERVICE_KEY,
                {"segment": "EXP-BEGIN", "time": "2026-11-11T08:15:00Z"},
                ttl_ms=5000)
            reply = envelope.get("resp", envelope)
            self.assertTrue(reply.get("ok"), reply)
            self.assertEqual(reply["cards"][2],
                             {"keyword": "ADCPOS", "value": 1.5,
                              "comment": "ADC position"})
            self.assertEqual(client.get(f"{peer}.segments"), "EXP-BEGIN")
            self.assertEqual(client.get(f"{peer}.requests"), 1)

            envelope = client._libby.rpc(  # pylint: disable=protected-access
                peer, SERVICE_KEY, {"segment": "NOPE", "time": "2026-11-11"},
                ttl_ms=5000)
            reply = envelope.get("resp", envelope)
            self.assertFalse(reply.get("ok"))
            self.assertIn("unknown segment", reply["error"])


if __name__ == "__main__":
    unittest.main()
