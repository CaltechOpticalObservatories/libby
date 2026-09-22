"""Integration tests: ``Client`` against a live ``LibbyDaemon``, per transport.

Every case runs once per transport, so a change that only holds over RabbitMQ
fails here. The ZMQ cases need no external service and always run; the RabbitMQ
cases skip unless a broker is reachable at ``amqp://localhost``.

There are no sleeps or readiness polls anywhere below, deliberately. Both
transports block in ``start()`` until they can be talked to, and
``test_first_read_needs_no_warmup`` asserts exactly that, so a regression in
the barrier fails loudly instead of being absorbed by a retry.
"""
from __future__ import annotations

import socket
import threading
import unittest
from typing import List, Optional, Tuple, Type

from libby import Client, KeywordError
from libby.client import DEFAULT_SELF_ID
from libby.daemon import LibbyDaemon
from libby.rabbitmq_transport import RabbitMQTransport

RABBITMQ_URL = "amqp://localhost"
GROUP_ID = "hsfei"
OTHER_GROUP_ID = "hscal"
RPC_TIMEOUT_S = 6.0
LISTING_TIMEOUT_S = 2.0
SERVE_STOP_TIMEOUT_S = 10.0
INITIAL_POSITION = 10.0


def _broker_available() -> bool:
    try:
        probe = RabbitMQTransport(peer_id="libby-test-probe", rabbitmq_url=RABBITMQ_URL)
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
    """Daemon exposing the keywords the client cases exercise."""

    group_id = GROUP_ID
    discovery_enabled = False

    def on_start(self, libby) -> None:
        state = {"pos": INITIAL_POSITION}
        self.keyword_registry.float(
            "positionvalue",
            getter=lambda: state["pos"],
            setter=lambda v: state.update(pos=v),
            units="mm",
        )
        self.keyword_registry.bool("isreferenced", getter=lambda: True)


class _LifecycleDaemon(LibbyDaemon):
    """Daemon registering its own shutdown trigger, as the fleet convention expects."""

    group_id = GROUP_ID
    discovery_enabled = False

    def on_start(self, libby) -> None:
        self.keyword_registry.trigger(
            "shutdown",
            action=self.request_stop,
            description="Gracefully stop this daemon.",
        )


class _RabbitFixtureDaemon(_FixtureDaemon):
    """Fixture daemon served over RabbitMQ."""

    peer_id = "pickofftestrmq"
    transport = "rabbitmq"
    rabbitmq_url = RABBITMQ_URL


class _ZmqFixtureDaemon(_FixtureDaemon):
    """Fixture daemon served over ZMQ; ``bind`` is assigned per instance."""

    peer_id = "pickofftestzmq"
    transport = "zmq"


class _RabbitLifecycleDaemon(_LifecycleDaemon):
    """Lifecycle daemon served over RabbitMQ."""

    peer_id = "lifecycletestrmq"
    transport = "rabbitmq"
    rabbitmq_url = RABBITMQ_URL


class _ZmqLifecycleDaemon(_LifecycleDaemon):
    """Lifecycle daemon served over ZMQ; ``bind`` is assigned per instance."""

    peer_id = "lifecycletestzmq"
    transport = "zmq"


def _started_daemon(
    daemon_cls: Type[_FixtureDaemon],
    daemon_peer_id: str,
    daemon_group_id: str,
    bind: Optional[str] = None,
) -> LibbyDaemon:
    """Start one fixture daemon under its own peer and group id."""
    daemon = daemon_cls()
    daemon.peer_id = daemon_peer_id
    daemon.group_id = daemon_group_id
    if bind is not None:
        daemon.bind = bind
    daemon.start()
    return daemon


# Nested inside a plain class so unittest's loader, which collects every
# module-level TestCase subclass, does not run the bases on their own.
class _Bases:  # pylint: disable=too-few-public-methods
    """Namespace for the transport-agnostic cases each transport subclasses."""

    class ClientCases(unittest.TestCase):
        """Client behaviour that must hold identically on every transport.

        Concrete subclasses supply ``client`` and ``peer`` from ``setUpClass``.
        """

        client: Client
        peer: str

        def _name(self, keyword: str) -> str:
            return f"{self.peer}.{keyword}"

        def setUp(self) -> None:  # pylint: disable=invalid-name
            # Restore the mutable keyword so the cases stay order-independent
            self.client.set(self._name("positionvalue"), INITIAL_POSITION,
                            timeout_s=RPC_TIMEOUT_S)

        def test_get_returns_current_value(self):
            """Read a float keyword."""
            self.assertEqual(
                self.client.get(self._name("positionvalue"), timeout_s=RPC_TIMEOUT_S),
                INITIAL_POSITION,
            )

        def test_set_then_get_round_trips(self):
            """Write a float keyword and read the applied value back."""
            self.assertEqual(
                self.client.set(self._name("positionvalue"), 42.0, timeout_s=RPC_TIMEOUT_S),
                42.0,
            )
            self.assertEqual(
                self.client.get(self._name("positionvalue"), timeout_s=RPC_TIMEOUT_S),
                42.0,
            )

        def test_show_includes_units(self):
            """Carry the keyword's units through in the full response."""
            resp = self.client.show(self._name("positionvalue"), timeout_s=RPC_TIMEOUT_S)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["units"], "mm")

        def test_set_on_read_only_keyword_raises_keyword_error(self):
            """Reject a write to a getter-only keyword."""
            with self.assertRaises(KeywordError):
                self.client.set(self._name("isreferenced"), False, timeout_s=RPC_TIMEOUT_S)

        def test_uptime_is_a_small_positive_integer(self):
            """Serve the uptime keyword LibbyDaemon registers for every peer."""
            uptime = self.client.get(self._name("uptime"), timeout_s=RPC_TIMEOUT_S)
            self.assertIsInstance(uptime, int)
            self.assertGreaterEqual(uptime, 0)

        def test_list_returns_qualified_names(self):
            """List matching keywords as names that feed straight back in."""
            names = self.client.list(self._name("is%"), timeout_s=RPC_TIMEOUT_S)
            self.assertIn(self._name("isreferenced"), names)
            self.assertEqual(
                self.client.get(names[0], timeout_s=RPC_TIMEOUT_S), True)

        def test_listing_advertises_bulk_read(self):
            """Report keys.read in the services field, over either transport."""
            listing = self.client.listing(self._name("%"), timeout_s=RPC_TIMEOUT_S)
            self.assertIn("keys.read", listing.services)
            self.assertIn(self._name("positionvalue"), listing.names)

        def test_describe_reports_type_and_units(self):
            """Read one keyword's metadata."""
            meta = self.client.describe(self._name("positionvalue"),
                                        timeout_s=RPC_TIMEOUT_S)
            self.assertEqual(meta["type"], "float")
            self.assertEqual(meta["units"], "mm")

        def test_read_answers_every_requested_name(self):
            """Read a batch keyed by qualified name, in the order asked."""
            wanted = [self._name("positionvalue"), self._name("isreferenced")]
            values = self.client.read(wanted, timeout_s=RPC_TIMEOUT_S)
            self.assertEqual(list(values), wanted)
            self.assertEqual(values[self._name("positionvalue")]["value"],
                             INITIAL_POSITION)
            self.assertEqual(values[self._name("positionvalue")]["units"], "mm")

        def test_read_reports_a_bad_name_without_raising(self):
            """Keep an unknown keyword from failing the rest of the batch."""
            values = self.client.read(
                [self._name("positionvalue"), self._name("nosuchkeyword")],
                timeout_s=RPC_TIMEOUT_S)
            self.assertTrue(values[self._name("positionvalue")]["ok"])
            self.assertFalse(values[self._name("nosuchkeyword")]["ok"])

        def test_read_merges_chunked_requests(self):
            """Merge several bounded requests into one result map."""
            wanted = [self._name("positionvalue"), self._name("isreferenced"),
                      self._name("uptime")]
            values = self.client.read(wanted, timeout_s=RPC_TIMEOUT_S, chunk_size=1)
            self.assertEqual(list(values), wanted)
            self.assertTrue(all(entry["ok"] for entry in values.values()))

    class PeerListingCases(unittest.TestCase):
        """Broadcast peer listing that must hold identically on every transport.

        Concrete subclasses start two fixture daemons in ``GROUP_ID`` and one
        in ``OTHER_GROUP_ID``, and supply ``client`` plus their qualified ids.
        """

        client: Client
        group_peers: Tuple[str, str]
        other_peer: str

        def test_peers_lists_every_daemon_in_the_group(self):
            """Find the group's daemons by wildcard, and no other group's."""
            found = self.client.peers(f"{GROUP_ID}.%", timeout_s=LISTING_TIMEOUT_S)
            for peer in self.group_peers:
                self.assertIn(peer, found)
            self.assertNotIn(self.other_peer, found)

        def test_peers_spans_groups_and_skips_the_client(self):
            """Find every daemon with %.% without listing the asking client."""
            found = self.client.peers("%.%", timeout_s=LISTING_TIMEOUT_S)
            for peer in (*self.group_peers, self.other_peer):
                self.assertIn(peer, found)
            self.assertNotIn(DEFAULT_SELF_ID, found)

        def test_peers_with_an_exact_id_confirms_one_daemon(self):
            """Resolve an exact <group>.<daemon> to just that daemon."""
            found = self.client.peers(self.group_peers[0], timeout_s=LISTING_TIMEOUT_S)
            self.assertEqual(found, [self.group_peers[0]])

        def test_list_across_daemons_returns_qualified_names(self):
            """List one keyword on every daemon of a group as names that feed back in."""
            names = self.client.list(f"{GROUP_ID}.%.positionvalue",
                                     timeout_s=LISTING_TIMEOUT_S)
            for peer in self.group_peers:
                self.assertIn(f"{peer}.positionvalue", names)
            self.assertNotIn(f"{self.other_peer}.positionvalue", names)
            self.assertEqual(
                self.client.get(f"{self.group_peers[0]}.positionvalue",
                                timeout_s=RPC_TIMEOUT_S),
                INITIAL_POSITION)

        def test_list_across_daemons_with_no_match_is_empty(self):
            """Return nothing, rather than raise, when no daemon has the keyword."""
            self.assertEqual(
                self.client.list(f"{GROUP_ID}.%.nosuchkeyword", timeout_s=LISTING_TIMEOUT_S),
                [])

        def test_peer_listings_carry_each_daemons_keywords(self):
            """Map each daemon to its keyword names from the same broadcast."""
            listings = self.client.peer_listings(f"{GROUP_ID}.%", timeout_s=LISTING_TIMEOUT_S)
            for peer in self.group_peers:
                self.assertIn("positionvalue", listings[peer])
                self.assertIn("uptime", listings[peer])

    class LifecycleCases(unittest.TestCase):
        """Daemon startup and shutdown guarantees, per transport."""

        def build_peer(self) -> Tuple[LibbyDaemon, Client, str]:
            """Return an unstarted daemon, a client for it, and its qualified id."""
            raise NotImplementedError

        def test_first_read_needs_no_warmup(self):
            """Answer a read issued the instant start() returns, with no wait.

            Both transports block in start() until they are reachable, so a
            caller never has to sleep. This is the guard on that: if the
            barrier regresses, this fails rather than every other case going
            flaky.
            """
            daemon, client, peer = self.build_peer()
            daemon.start()
            try:
                self.assertIsInstance(
                    client.get(f"{peer}.uptime", timeout_s=RPC_TIMEOUT_S), int)
            finally:
                client.close()
                daemon.stop()

        def test_shutdown_trigger_stops_the_daemon(self):
            """Stop a serving daemon by writing its shutdown keyword."""
            daemon, client, peer = self.build_peer()
            # start() here rather than letting the thread do it: serve() calls
            # start() and returns early when already started, so the daemon is
            # reachable before the thread exists and there is nothing to await.
            daemon.start()
            serve_thread = threading.Thread(target=daemon.serve, daemon=True)
            serve_thread.start()
            try:
                client.set(f"{peer}.shutdown", 1, timeout_s=RPC_TIMEOUT_S)
                serve_thread.join(timeout=SERVE_STOP_TIMEOUT_S)
                self.assertFalse(serve_thread.is_alive())
            finally:
                client.close()
                daemon.stop()


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class RabbitMQClientTests(_Bases.ClientCases):
    """Client cases over RabbitMQ."""

    @classmethod
    def setUpClass(cls):
        cls.daemon = _RabbitFixtureDaemon()
        cls.daemon.start()
        cls.peer = f"{GROUP_ID}.{_RabbitFixtureDaemon.peer_id}"
        cls.client = Client.rabbitmq(rabbitmq_url=RABBITMQ_URL)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.daemon.stop()


class ZmqClientTests(_Bases.ClientCases):
    """Client cases over ZMQ."""

    @classmethod
    def setUpClass(cls):
        endpoint = _free_endpoint()
        cls.daemon = _ZmqFixtureDaemon()
        cls.daemon.bind = endpoint
        cls.daemon.start()
        cls.peer = f"{GROUP_ID}.{_ZmqFixtureDaemon.peer_id}"
        cls.client = Client.zmq(bind=_free_endpoint(), address_book={cls.peer: endpoint})

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.daemon.stop()


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class RabbitMQPeerListingTests(_Bases.PeerListingCases):
    """Peer listing cases over RabbitMQ."""

    daemons: List[LibbyDaemon]

    @classmethod
    def setUpClass(cls):
        cls.daemons = [
            _started_daemon(_RabbitFixtureDaemon, "listingonermq", GROUP_ID),
            _started_daemon(_RabbitFixtureDaemon, "listingtwormq", GROUP_ID),
            _started_daemon(_RabbitFixtureDaemon, "listingotherrmq", OTHER_GROUP_ID),
        ]
        cls.group_peers = (f"{GROUP_ID}.listingonermq", f"{GROUP_ID}.listingtwormq")
        cls.other_peer = f"{OTHER_GROUP_ID}.listingotherrmq"
        cls.client = Client.rabbitmq(rabbitmq_url=RABBITMQ_URL)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for daemon in cls.daemons:
            daemon.stop()


class ZmqPeerListingTests(_Bases.PeerListingCases):
    """Peer listing cases over ZMQ, where the address book is what gets asked."""

    daemons: List[LibbyDaemon]

    @classmethod
    def setUpClass(cls):
        groups = {"listingonezmq": GROUP_ID, "listingtwozmq": GROUP_ID,
                  "listingotherzmq": OTHER_GROUP_ID}
        address_book = {f"{group}.{name}": _free_endpoint() for name, group in groups.items()}
        cls.daemons = [
            _started_daemon(_ZmqFixtureDaemon, name, group, bind=address_book[f"{group}.{name}"])
            for name, group in groups.items()
        ]
        cls.group_peers = (f"{GROUP_ID}.listingonezmq", f"{GROUP_ID}.listingtwozmq")
        cls.other_peer = f"{OTHER_GROUP_ID}.listingotherzmq"
        cls.client = Client.zmq(bind=_free_endpoint(), address_book=address_book)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for daemon in cls.daemons:
            daemon.stop()


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class RabbitMQLifecycleTests(_Bases.LifecycleCases):
    """Lifecycle cases over RabbitMQ."""

    def build_peer(self) -> Tuple[LibbyDaemon, Client, str]:
        return (_RabbitLifecycleDaemon(),
                Client.rabbitmq(rabbitmq_url=RABBITMQ_URL),
                f"{GROUP_ID}.{_RabbitLifecycleDaemon.peer_id}")


class ZmqLifecycleTests(_Bases.LifecycleCases):
    """Lifecycle cases over ZMQ."""

    def build_peer(self) -> Tuple[LibbyDaemon, Client, str]:
        endpoint = _free_endpoint()
        daemon = _ZmqLifecycleDaemon()
        daemon.bind = endpoint
        peer = f"{GROUP_ID}.{_ZmqLifecycleDaemon.peer_id}"
        client = Client.zmq(bind=_free_endpoint(), address_book={peer: endpoint})
        return daemon, client, peer


if __name__ == "__main__":
    unittest.main()
