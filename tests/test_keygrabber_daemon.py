"""End-to-end tests: the keygrabber against a live peer, on each transport.

Every case runs once per transport. The ZMQ cases need no external service and
always run; the RabbitMQ cases skip unless a broker is reachable. RabbitMQ
matters most here: it is the default transport, and the one where the worker
pool issues concurrent requests over a single pika connection.

A recording sink stands in for the database, so no server is needed either.
"""
from __future__ import annotations

import socket
import threading
import time
import unittest
from typing import List, Sequence

from libby.daemon import LibbyDaemon
from libby.keygrabber import KeygrabberDaemon, Sample
from libby.rabbitmq_transport import RabbitMQTransport

SETTLE_TIMEOUT_S = 15.0
RABBITMQ_URL = "amqp://localhost"


def _broker_available() -> bool:
    try:
        probe = RabbitMQTransport(peer_id="keygrabber-test-probe",
                                  rabbitmq_url=RABBITMQ_URL)
        probe.stop()
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _free_endpoint() -> str:
    """Return a loopback ZMQ endpoint on a port the OS just reported free."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{probe.getsockname()[1]}"


class _FixtureDaemon(LibbyDaemon):
    """Peer the keygrabber reads, with one keyword of each interesting shape."""

    group_id = "hsfei"
    discovery_enabled = False

    def on_start(self, libby) -> None:
        self.keyword_registry.float("positionvalue", getter=lambda: 7.5,
                                    units="mm")
        self.keyword_registry.bool("isconnected", getter=lambda: True)
        self.keyword_registry.string("status", getter=lambda: "Ready")


class _ZmqFixtureDaemon(_FixtureDaemon):
    """Target peer served over ZMQ; ``bind`` is assigned per instance."""

    peer_id = "kgtargetzmq"
    transport = "zmq"


class _RabbitFixtureDaemon(_FixtureDaemon):
    """Target peer served over RabbitMQ."""

    peer_id = "kgtargetrmq"
    transport = "rabbitmq"
    rabbitmq_url = RABBITMQ_URL


class _RecordingSink:
    """Collects everything the keygrabber writes, in place of a database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.samples: List[Sample] = []

    def connect(self) -> None:
        """Nothing to open."""

    def is_connected(self) -> bool:
        """Always reachable."""
        return True

    def write(self, samples: Sequence[Sample]) -> int:
        """Record a batch and report it all stored."""
        with self._lock:
            self.samples.extend(samples)
        return len(samples)

    def close(self) -> None:
        """Nothing to release."""

    def keywords(self) -> List[str]:
        """Return the keyword names recorded so far."""
        return [sample.keyword for sample in self.snapshot()]

    def snapshot(self) -> List[Sample]:
        """Return a copy of the recorded samples."""
        with self._lock:
            return list(self.samples)


class _TestKeygrabber(KeygrabberDaemon):
    """Keygrabber writing to a recording sink instead of InfluxDB.

    ``from_config`` constructs a daemon with no arguments, so the sink is
    assigned after construction and before ``start``.
    """

    sink: _RecordingSink

    def make_sink(self) -> _RecordingSink:
        """Return the injected sink."""
        return self.sink


# Nested inside a plain class so unittest's loader, which collects every
# module-level TestCase subclass, does not run the bases on their own.
class _Bases:  # pylint: disable=too-few-public-methods
    """Namespace for the transport-agnostic cases each transport subclasses."""

    class KeygrabberCases(unittest.TestCase):
        """The daemon reads a real peer on a cadence and writes what it read.

        Concrete subclasses start their own target peer and supply the matching
        keygrabber config.
        """

        target_peer: str

        def grabber_config(self) -> dict:
            """Return the transport-specific keygrabber config."""
            raise NotImplementedError

        def setUp(self) -> None:  # pylint: disable=invalid-name
            self.sink = _RecordingSink()
            self.grabber = _TestKeygrabber.from_config(self.grabber_config())
            self.grabber.sink = self.sink

        def tearDown(self) -> None:  # pylint: disable=invalid-name
            self.grabber.stop()

        def _await_samples(self, minimum: int = 3) -> None:
            """Block until enough samples arrive, rather than sleeping a guess."""
            deadline = time.monotonic() + SETTLE_TIMEOUT_S
            while time.monotonic() < deadline:
                if len(self.sink.keywords()) >= minimum:
                    return
                time.sleep(0.05)
            self.fail(f"only {len(self.sink.keywords())} samples within "
                      f"{SETTLE_TIMEOUT_S}s: {self.sink.keywords()}")

        def test_collects_the_selected_keywords(self):
            """Read every selected keyword and write it to the sink."""
            self.grabber.start()
            self._await_samples()
            self.assertEqual(set(self.sink.keywords()),
                             {"positionvalue", "isconnected", "status"})

        def test_default_exclusions_are_not_collected(self):
            """Keep uptime and lasterror out of a select-everything collection."""
            self.grabber.start()
            self._await_samples()
            recorded = set(self.sink.keywords())
            self.assertNotIn("uptime", recorded)
            self.assertNotIn("lasterror", recorded)

        def test_samples_carry_their_tags_and_values(self):
            """Carry group, peer, units and value through to the sink."""
            self.grabber.start()
            self._await_samples()
            sample = next(s for s in self.sink.snapshot()
                          if s.keyword == "positionvalue")
            group, daemon = self.target_peer.split(".")
            self.assertEqual((sample.group, sample.peer), (group, daemon))
            self.assertEqual(sample.value, 7.5)
            self.assertEqual(sample.units, "mm")

        def test_points_written_is_counted(self):
            """Report what the sink actually stored."""
            self.grabber.start()
            self._await_samples()
            self.assertGreater(self.grabber.counters.points_written, 0)
            self.assertEqual(self.grabber.counters.write_errors, 0)

        def test_repeats_on_the_configured_cadence(self):
            """Read again on the next interval rather than once at startup."""
            self.grabber.start()
            self._await_samples(minimum=3)
            first = len(self.sink.keywords())
            deadline = time.monotonic() + SETTLE_TIMEOUT_S
            while time.monotonic() < deadline:
                if len(self.sink.keywords()) > first:
                    return
                time.sleep(0.05)
            self.fail("the collection never ran a second tick")

        def test_stop_is_clean_with_nothing_left_queued(self):
            """Drain on the way out, so the last tick is not dropped."""
            self.grabber.start()
            self._await_samples()
            self.grabber.stop()
            self.assertEqual(self.grabber.counters.dropped_batches, 0)


def _collections(peer: str) -> dict:
    """Return a one-collection config reading everything on ``peer``."""
    return {
        "target": {
            "peer": peer,
            "keywords": ["%"],
            "interval_s": 0.5,
            "timeout_s": 0.3,
            "refresh_s": 60.0,
        },
    }


class ZmqKeygrabberTests(_Bases.KeygrabberCases):
    """Keygrabber cases over ZMQ."""

    target_peer = f"hsfei.{_ZmqFixtureDaemon.peer_id}"

    @classmethod
    def setUpClass(cls):
        cls.endpoint = _free_endpoint()
        cls.target = _ZmqFixtureDaemon()
        cls.target.bind = cls.endpoint
        cls.target.start()

    @classmethod
    def tearDownClass(cls):
        cls.target.stop()

    def grabber_config(self) -> dict:
        return {
            "peer_id": "keygrabberzmq",
            "group_id": "hispec",
            "transport": "zmq",
            "bind": _free_endpoint(),
            "address_book": {self.target_peer: self.endpoint},
            "discovery_enabled": False,
            "sink": {"type": "recording"},
            "workers": 2,
            "collections": _collections(self.target_peer),
        }


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class RabbitMQKeygrabberTests(_Bases.KeygrabberCases):
    """Keygrabber cases over RabbitMQ, the default transport."""

    target_peer = f"hsfei.{_RabbitFixtureDaemon.peer_id}"

    @classmethod
    def setUpClass(cls):
        cls.target = _RabbitFixtureDaemon()
        cls.target.start()

    @classmethod
    def tearDownClass(cls):
        cls.target.stop()

    def grabber_config(self) -> dict:
        return {
            "peer_id": "keygrabberrmq",
            "group_id": "hispec",
            "transport": "rabbitmq",
            "rabbitmq_url": RABBITMQ_URL,
            "discovery_enabled": False,
            "sink": {"type": "recording"},
            "workers": 2,
            "collections": _collections(self.target_peer),
        }


if __name__ == "__main__":
    unittest.main()
