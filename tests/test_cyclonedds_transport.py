"""Unit tests for CycloneDdsTransport: envelope encoding and delivery.

Delivery cases run real participants on a random DDS domain, so concurrent
runs cannot hear each other. Skipped when cyclonedds is not installed.
"""
from __future__ import annotations

import dataclasses
import queue
import random
import unittest
from typing import List, Tuple

from bamboo import Envelope, Message, MessageBuilder

try:
    from libby.cyclonedds_transport import CycloneDdsTransport, DdsEnvelope, to_message, to_sample
except ImportError:  # cyclonedds is an optional extra
    CycloneDdsTransport = None

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
DELIVERY_TIMEOUT_S = 5.0
SILENCE_S = 0.5
MAX_DOMAIN_ID = 230


def _wire_round_trip(msg: Message) -> Message:
    """Encode, serialize as DDS would, and decode."""
    return to_message(DdsEnvelope.deserialize(to_sample(msg).serialize()))


def _request(source: str, dest: str) -> Message:
    return MessageBuilder(source).req("positionvalue", {"value": 1.5}).to(dest).build()


@unittest.skipIf(CycloneDdsTransport is None, "cyclonedds not installed")
class EnvelopeEncodingTests(unittest.TestCase):
    """bamboo messages survive the trip through the DDS sample type."""

    def test_every_field_round_trips(self):
        """Carry every envelope field, the payload and the binary part unchanged."""
        msg = (MessageBuilder("hsfei.client").req("positionvalue", {"value": [1, "a", None]})
               .to("hsfei.pickoff").trace(TRACEPARENT, "vendor=1")
               .binary(b"\x00\n\n\xff").build())
        self.assertEqual(_wire_round_trip(msg), msg)

    def test_absent_optional_fields_stay_absent(self):
        """Decode empty strings and an empty binary back to None."""
        msg = MessageBuilder("hsfei.pickoff").pub("alerts", {}).to(None).build()
        decoded = _wire_round_trip(msg)
        self.assertIsNone(decoded.env.destid)
        self.assertIsNone(decoded.env.traceparent)
        self.assertIsNone(decoded.binary)
        self.assertEqual(decoded, msg)

    def test_sample_type_covers_every_envelope_field(self):
        """Fail when bamboo adds an envelope field the DDS type does not carry."""
        envelope_fields = {field.name for field in dataclasses.fields(Envelope)}
        sample_fields = {field.name for field in dataclasses.fields(DdsEnvelope)}
        self.assertEqual(sample_fields, envelope_fields | {"binary"})


@unittest.skipIf(CycloneDdsTransport is None, "cyclonedds not installed")
class DeliveryTests(unittest.TestCase):
    """Direct messages reach only their addressee; broadcasts reach everyone."""

    def setUp(self) -> None:
        domain_id = random.randint(1, MAX_DOMAIN_ID)
        self.inboxes: dict[str, "queue.Queue[Tuple[str, Message]]"] = {}
        self.transports = {}
        for peer_id in ("hsfei.client", "hsfei.pickoff", "hsfei.adc"):
            transport = CycloneDdsTransport(peer_id, domain_id=domain_id)
            inbox: "queue.Queue[Tuple[str, Message]]" = queue.Queue()
            transport.on_receive(lambda source, msg, inbox=inbox: inbox.put((source, msg)))
            self.inboxes[peer_id] = inbox
            self.transports[peer_id] = transport
        for transport in self.transports.values():
            transport.start()

    def tearDown(self) -> None:
        for transport in self.transports.values():
            transport.stop()

    def _received(self, peer_id: str, timeout_s: float) -> List[Tuple[str, Message]]:
        inbox = self.inboxes[peer_id]
        try:
            first = inbox.get(timeout=timeout_s)
        except queue.Empty:
            return []
        return [first] + [inbox.get_nowait() for _ in range(inbox.qsize())]

    def test_direct_message_reaches_its_addressee(self):
        """Deliver a direct message with its source and envelope intact."""
        msg = _request("hsfei.client", "hsfei.pickoff")
        self.transports["hsfei.client"].send("peer:hsfei.pickoff", msg)
        self.assertEqual(self._received("hsfei.pickoff", DELIVERY_TIMEOUT_S),
                         [("peer:hsfei.client", msg)])

    def test_direct_message_reaches_no_one_else(self):
        """Keep a direct message out of other peers' partitions."""
        msg = _request("hsfei.client", "hsfei.pickoff")
        self.transports["hsfei.client"].send("peer:hsfei.pickoff", msg)
        self._received("hsfei.pickoff", DELIVERY_TIMEOUT_S)
        self.assertEqual(self._received("hsfei.adc", SILENCE_S), [])
        self.assertEqual(self._received("hsfei.client", SILENCE_S), [])

    def test_broadcast_reaches_every_peer_including_the_sender(self):
        """Deliver a broadcast everywhere, as the RabbitMQ fanout does."""
        msg = MessageBuilder("hsfei.client").pub("alerts", {"level": 1}).to(None).build()
        self.transports["hsfei.client"].send("broadcast:*", msg)
        for peer_id in self.transports:
            self.assertEqual(self._received(peer_id, DELIVERY_TIMEOUT_S),
                             [("peer:hsfei.client", msg)], peer_id)

    def test_send_after_stop_is_dropped(self):
        """Drop a late send rather than raising or delivering it."""
        self.transports["hsfei.client"].stop()
        self.transports["hsfei.client"].send(
            "peer:hsfei.pickoff", _request("hsfei.client", "hsfei.pickoff"))
        self.assertEqual(self._received("hsfei.pickoff", SILENCE_S), [])


if __name__ == "__main__":
    unittest.main()
