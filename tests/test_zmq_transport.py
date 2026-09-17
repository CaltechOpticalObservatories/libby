"""Unit tests for ZmqTransport's teardown behaviour.

Needs no peer on the other end: sending over ZMQ queues locally, so these
assert on the sockets this transport owns.
"""
from __future__ import annotations

import socket
import unittest

from libby.zmq_transport import ZmqTransport

# These assert on the sockets the transport owns, which are private
# pylint: disable=protected-access


def _free_endpoint() -> str:
    """Return a loopback ZMQ endpoint on a port the OS just reported free."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{probe.getsockname()[1]}"


class SendAfterStopTests(unittest.TestCase):
    """A send arriving after stop must not resurrect sockets.

    Sending creates a DEALER per peer on demand, and ``stop()`` closes the ones
    that exist. A later send would create another that nothing ever closes,
    which is reachable in practice because bamboo's discovery announces from a
    thread its own ``stop()`` does not join.
    """

    def setUp(self) -> None:
        self.peer_endpoint = _free_endpoint()
        self.transport = ZmqTransport(bind_router=_free_endpoint(),
                                      address_book={"hsfei.peer": self.peer_endpoint},
                                      my_id="hsfei.tester")
        self.transport.start()

    def test_broadcast_before_stop_opens_a_dealer(self):
        """Confirm the leak is reachable: a live broadcast does open one."""
        self.transport.send("broadcast:*", b"frame")
        self.assertEqual(list(self.transport._dealers), ["hsfei.peer"])
        self.transport.stop()

    def test_stop_closes_the_dealers_it_opened(self):
        """Leave nothing behind for a transport that was used and stopped."""
        self.transport.send("broadcast:*", b"frame")
        self.transport.stop()
        self.assertEqual(self.transport._dealers, {})

    def test_broadcast_after_stop_opens_nothing(self):
        """Drop a late announce rather than opening a socket nothing will close."""
        self.transport.stop()
        self.transport.send("broadcast:*", b"frame")
        self.assertEqual(self.transport._dealers, {})

    def test_direct_send_after_stop_opens_nothing(self):
        """Drop a late direct send for the same reason."""
        self.transport.stop()
        self.transport.send("peer:hsfei.peer", b"frame")
        self.assertEqual(self.transport._dealers, {})

    def test_stop_is_idempotent(self):
        """Tolerate a second stop, as LibbyDaemon's teardown can retry."""
        self.transport.stop()
        self.transport.stop()
        self.assertEqual(self.transport._dealers, {})


if __name__ == "__main__":
    unittest.main()
