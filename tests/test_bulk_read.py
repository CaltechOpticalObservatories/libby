"""Unit tests for bulk reads: the ``keys.read`` service and ``Client.read``.

``keys.read`` is only reachable through a transport, so its selection and error
semantics are exercised by calling the handler directly; ``Client.read`` runs
against a recording stand-in for ``Libby`` so the request shapes it produces
are visible. The end-to-end path over both transports lives in
test_client_integration.
"""
from __future__ import annotations

import socket
import unittest
from typing import Any, Dict, List, Tuple

from libby import BoolKeyword, Client, FloatKeyword, Libby, StringKeyword, TriggerKeyword

# These tests deliberately reach for the handler and the injected transport
# pylint: disable=protected-access


def _free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{probe.getsockname()[1]}"


def _boom() -> str:
    raise RuntimeError("hardware unreachable")


class KeysReadTests(unittest.TestCase):
    """Selection and per-keyword error handling in the keys.read service."""

    def setUp(self) -> None:
        self.libby = Libby.zmq(self_id="keysreadtest", bind=_free_endpoint(),
                               address_book={}, keys=[])
        self.libby.register_keywords([
            FloatKeyword("positionvalue", getter=lambda: 7.5, units="mm"),
            BoolKeyword("isconnected", getter=lambda: True),
            StringKeyword("broken", getter=_boom),
            FloatKeyword("target", setter=lambda v: None),
            TriggerKeyword("halt", action=lambda: None),
        ])

    def tearDown(self) -> None:
        self.libby.stop()

    def _read(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.libby._keys_read(payload, {})

    def test_pattern_returns_readable_keywords_with_units(self):
        """Carry each keyword's own show response, units included."""
        resp = self._read({"pattern": "%"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["values"]["positionvalue"],
                         {"ok": True, "value": 7.5, "units": "mm"})
        self.assertEqual(resp["values"]["isconnected"], {"ok": True, "value": True})

    def test_pattern_skips_write_only_keywords(self):
        """Omit setter-only keywords and triggers, which have nothing to show."""
        values = self._read({"pattern": "%"})["values"]
        self.assertNotIn("target", values)
        self.assertNotIn("halt", values)

    def test_pattern_narrows_the_selection(self):
        """Honour a wildcard pattern rather than always returning everything."""
        self.assertEqual(list(self._read({"pattern": "is%"})["values"]), ["isconnected"])

    def test_failing_getter_is_reported_per_keyword(self):
        """Keep a raising getter from failing the whole batch."""
        resp = self._read({"pattern": "%"})
        self.assertTrue(resp["ok"])
        broken = resp["values"]["broken"]
        self.assertFalse(broken["ok"])
        self.assertIn("hardware unreachable", broken["error"])

    def test_explicit_names_report_unknown_keywords_inline(self):
        """Answer a bad name with its own error, not a failed batch."""
        values = self._read({"names": ["positionvalue", "nosuchkeyword"]})["values"]
        self.assertTrue(values["positionvalue"]["ok"])
        self.assertFalse(values["nosuchkeyword"]["ok"])
        self.assertIn("unknown keyword", values["nosuchkeyword"]["error"])

    def test_explicitly_named_write_only_keyword_is_answered(self):
        """Report why a write-only keyword cannot be shown when asked by name."""
        values = self._read({"names": ["target"]})["values"]
        self.assertFalse(values["target"]["ok"])

    def test_non_string_pattern_is_rejected(self):
        """Reject a pattern that is not a string."""
        self.assertFalse(self._read({"pattern": 5})["ok"])

    def test_names_must_be_a_list_of_strings(self):
        """Reject a names payload that is not a list of strings."""
        for bad in ("positionvalue", [1, 2], {"a": 1}):
            with self.subTest(names=bad):
                self.assertFalse(self._read({"names": bad})["ok"])

    def test_empty_names_returns_an_empty_value_map(self):
        """Treat an empty request as an empty answer, not as select-everything."""
        self.assertEqual(self._read({"names": []}), {"ok": True, "values": {}})


class KeysListServicesTests(unittest.TestCase):
    """The services field keys.list reports for capability detection."""

    def setUp(self) -> None:
        self.libby = Libby.zmq(self_id="keyslisttest", bind=_free_endpoint(),
                               address_book={}, keys=[])
        self.libby.register_keywords([
            FloatKeyword("positionvalue", getter=lambda: 1.0),
            BoolKeyword("isconnected", getter=lambda: True),
        ])

    def tearDown(self) -> None:
        self.libby.stop()

    def _list(self, pattern: str = "%") -> Dict[str, Any]:
        return self.libby._keys_list({"pattern": pattern}, {})

    def test_meta_services_are_reported(self):
        """Advertise keys.read, which cannot be probed for and is not in matches."""
        services = self._list()["services"]
        self.assertIn("keys.read", services)
        self.assertIn("keys.list", services)
        self.assertIn("keys.describe", services)

    def test_keywords_are_not_repeated_as_services(self):
        """Keep keyword names in matches only, so the two fields stay disjoint."""
        response = self._list()
        self.assertIn("positionvalue", response["matches"])
        self.assertNotIn("positionvalue", response["services"])

    def test_services_are_independent_of_the_pattern(self):
        """Report capabilities even when the pattern matches no keyword."""
        response = self._list("nomatch%")
        self.assertEqual(response["matches"], [])
        self.assertIn("keys.read", response["services"])

    def test_daemon_registered_services_are_reported(self):
        """Include a peer's own RPC services, not just the keys.* meta-services."""
        self.libby.serve_keys(["recalibrate"], lambda payload, ctx: {"ok": True})
        self.assertIn("recalibrate", self._list()["services"])


class _RecordingLibby:  # pylint: disable=too-few-public-methods
    """Stands in for Libby, recording rpc calls and replying from a canned map."""

    def __init__(self, values: Dict[str, Dict[str, Any]], dead_peers: Tuple[str, ...] = ()):
        self.calls: List[Tuple[str, str, Tuple[str, ...]]] = []
        self._values = values
        self._dead_peers = dead_peers

    # ttl_ms is unused but kept so the signature matches Libby.rpc
    def rpc(self, peer_id: str, key: str, payload: Dict[str, Any],
            ttl_ms: int = 8000) -> Dict[str, Any]:  # pylint: disable=unused-argument
        """Record the call, then answer it from the canned map."""
        names = tuple(payload.get("names", ()))
        self.calls.append((peer_id, key, names))
        if peer_id in self._dead_peers:
            return {"status": "timeout"}
        return {"status": "delivered", "resp": {"ok": True, "values": {
            name: self._values[name] for name in names if name in self._values
        }}}


class _ListingLibby:  # pylint: disable=too-few-public-methods
    """Stands in for Libby, answering keys.list with a canned response."""

    def __init__(self, response: Dict[str, Any]):
        self._response = response

    def rpc(self, peer_id: str, key: str,
            payload: Dict[str, Any], ttl_ms: int = 8000) -> Dict[str, Any]:
        """Answer with the canned keys.list response."""
        # Arguments are ignored; the signature exists to match Libby.rpc
        # pylint: disable=unused-argument
        return {"status": "delivered", "resp": {"ok": True, **self._response}}


class ClientListingTests(unittest.TestCase):
    """Capability detection through Client.listing."""

    def test_names_are_qualified_and_services_reported(self):
        """Qualify matches for reuse, and pass the services field through."""
        client = Client(_ListingLibby({
            "matches": ["positionvalue", "isconnected"],
            "services": ["keys.describe", "keys.list", "keys.read"],
        }))
        listing = client.listing("hsfei.adc.%")
        self.assertEqual(listing.names,
                         ("hsfei.adc.positionvalue", "hsfei.adc.isconnected"))
        self.assertIn("keys.read", listing.services)

    def test_peer_omitting_services_reports_none(self):
        """Read an older peer's silence as no bulk read, not as an error.

        This is the negative signal the whole field exists for: knows_key
        cannot answer it, and probing keys.read is indistinguishable from a
        timeout.
        """
        client = Client(_ListingLibby({"matches": ["positionvalue"]}))
        listing = client.listing("hsfei.adc.%")
        self.assertEqual(listing.services, ())
        self.assertNotIn("keys.read", listing.services)

    def test_list_returns_names_only(self):
        """Keep the simple list contract on top of the richer listing call."""
        client = Client(_ListingLibby({
            "matches": ["positionvalue"], "services": ["keys.read"],
        }))
        self.assertEqual(client.list("hsfei.adc.%"), ["hsfei.adc.positionvalue"])


class ClientReadTests(unittest.TestCase):
    """Request batching, chunking and failure spreading in Client.read."""

    def setUp(self) -> None:
        self.values = {
            "positionvalue": {"ok": True, "value": 7.5, "units": "mm"},
            "isconnected": {"ok": True, "value": True},
            "uptime": {"ok": True, "value": 12},
        }

    def _client(self, dead_peers: Tuple[str, ...] = ()) -> Tuple[Client, _RecordingLibby]:
        libby = _RecordingLibby(self.values, dead_peers)
        return Client(libby), libby

    def test_one_request_per_peer(self):
        """Group names by peer so each peer is asked exactly once."""
        client, libby = self._client()
        result = client.read(["hsfei.adc.positionvalue",
                              "hsfei.adc.isconnected",
                              "hsfei.atcp.uptime"])
        self.assertEqual([(peer, key) for peer, key, _ in libby.calls],
                         [("hsfei.adc", "keys.read"), ("hsfei.atcp", "keys.read")])
        self.assertEqual(libby.calls[0][2], ("positionvalue", "isconnected"))
        self.assertEqual(result["hsfei.adc.positionvalue"]["value"], 7.5)
        self.assertEqual(result["hsfei.atcp.uptime"]["value"], 12)

    def test_chunking_splits_one_peer_across_requests(self):
        """Split a long name list into bounded requests and merge the answers."""
        client, libby = self._client()
        names = ["hsfei.adc.positionvalue", "hsfei.adc.isconnected", "hsfei.adc.uptime"]
        result = client.read(names, chunk_size=2)
        self.assertEqual([call[2] for call in libby.calls],
                         [("positionvalue", "isconnected"), ("uptime",)])
        self.assertEqual(list(result), names)

    def test_chunk_size_must_be_positive(self):
        """Refuse a chunk size that could never make progress."""
        client, _ = self._client()
        with self.assertRaises(ValueError):
            client.read(["hsfei.adc.uptime"], chunk_size=0)

    def test_dead_peer_fails_only_its_own_names(self):
        """Spread a peer-level failure across its names, sparing other peers."""
        client, _ = self._client(dead_peers=("hsfei.adc",))
        result = client.read(["hsfei.adc.positionvalue", "hsfei.atcp.uptime"])
        self.assertFalse(result["hsfei.adc.positionvalue"]["ok"])
        self.assertIn("timed out", result["hsfei.adc.positionvalue"]["error"])
        self.assertTrue(result["hsfei.atcp.uptime"]["ok"])

    def test_name_missing_from_the_response_is_reported(self):
        """Fill in a name the peer answered nothing for, so keys never vanish."""
        client, _ = self._client()
        result = client.read(["hsfei.adc.notinresponse"])
        self.assertFalse(result["hsfei.adc.notinresponse"]["ok"])
        self.assertIn("missing", result["hsfei.adc.notinresponse"]["error"])


if __name__ == "__main__":
    unittest.main()
