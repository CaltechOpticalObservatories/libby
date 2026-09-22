"""Unit tests for which broadcast replies ``Client.peer_listings`` keeps.

Every ``Libby`` serves ``keys.list``, so clients answer the broadcast too and
have to be filtered out. The reply shapes are produced by a stand-in here; the
end-to-end path over both transports lives in test_client_integration.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from libby import Client
from libby.libby import BroadcastReply


def _reply(peer_id: str, **payload: Any) -> BroadcastReply:
    return BroadcastReply(peer_id, {"ok": True, "matches": ["uptime"], **payload})


class _BroadcastLibby:  # pylint: disable=too-few-public-methods
    """Stands in for Libby, answering a broadcast with canned replies."""

    def __init__(self, replies: List[BroadcastReply]):
        self._replies = replies

    def broadcast_request(self, key: str, payload: Dict[str, Any],
                          timeout_s: float = 1.0) -> List[BroadcastReply]:
        """Answer with the canned replies."""
        # Arguments are ignored; the signature exists to match Libby
        # pylint: disable=unused-argument
        return self._replies


class PeerListingFilterTests(unittest.TestCase):
    """Which replies count as a daemon."""

    def _peers(self, *replies: BroadcastReply) -> List[str]:
        return Client(_BroadcastLibby(list(replies))).peers("hsfei.%")

    def test_keeps_a_daemon(self):
        self.assertEqual(self._peers(_reply("hsfei.adc", is_daemon=True)), ["hsfei.adc"])

    def test_drops_a_client(self):
        self.assertEqual(self._peers(_reply("hsfei.impostor", is_daemon=False)), [])

    def test_drops_a_reply_predating_the_flag(self):
        # A libby old enough to omit is_daemon cannot prove it is one
        self.assertEqual(self._peers(_reply("hsfei.adc")), [])

    def test_drops_a_failed_reply(self):
        self.assertEqual(
            self._peers(BroadcastReply("hsfei.adc", {"ok": False, "error": "nope"})), [])

    def test_filters_before_matching_the_pattern(self):
        found = self._peers(
            _reply("hsfei.adc", is_daemon=True),
            _reply("hsfei.impostor", is_daemon=False),
            _reply("hscal.hkettherm", is_daemon=True),
        )
        self.assertEqual(found, ["hsfei.adc"])

    def test_peer_listings_carry_the_matches(self):
        listings = Client(_BroadcastLibby([
            _reply("hsfei.adc", is_daemon=True, matches=["uptime", "isconnected"]),
            _reply("hsfei.impostor", is_daemon=False, matches=["uptime"]),
        ])).peer_listings("hsfei.%")
        self.assertEqual(listings, {"hsfei.adc": ["uptime", "isconnected"]})


if __name__ == "__main__":
    unittest.main()
