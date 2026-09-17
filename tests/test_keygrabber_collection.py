"""Unit tests for Collection: resolution, refresh, and one tick's samples."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from libby import KeyListing, LibbyTimeout
from libby.keygrabber import Collection, parse_config

TIMESTAMP = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _collection_config(**entry: Any):
    config = {
        "collections": {
            "adc": {"peer": "hsfei.adc", "keywords": ["%"], **entry},
        },
    }
    return parse_config(config).collections[0]


class _FakeClock:
    """Manually advanced clock, so refresh windows need no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class _FakeClient:
    """Stands in for Client, serving a canned listing and canned reads."""

    # Timeout and chunking arguments are accepted to match Client's signatures
    # pylint: disable=unused-argument

    def __init__(
        self,
        names: Sequence[str] = ("positionvalue", "isconnected"),
        services: Sequence[str] = ("keys.read",),
        values: Optional[Dict[str, Dict[str, Any]]] = None,
        timeouts_after: Optional[int] = None,
    ) -> None:
        self.names = list(names)
        self.services = list(services)
        self.values = values or {}
        self.timeouts_after = timeouts_after
        self.read_calls: List[Tuple[str, ...]] = []
        self.show_calls: List[str] = []
        self.listings = 0

    def listing(self, pattern: str, *, timeout_s: float = 3.0) -> KeyListing:
        """Return the canned listing, qualified like the real client does."""
        self.listings += 1
        prefix = pattern.rsplit(".", 1)[0]
        return KeyListing(
            names=tuple(f"{prefix}.{name}" for name in self.names),
            services=tuple(self.services),
        )

    def read(self, names: Sequence[str], *, timeout_s: float = 3.0,
             chunk_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """Record the batch and answer from the canned values."""
        self.read_calls.append(tuple(names))
        return {name: self._value(name) for name in names}

    def show(self, name: str, *, timeout_s: float = 3.0) -> Dict[str, Any]:
        """Answer one keyword, timing out once past the configured point."""
        self.show_calls.append(name)
        if self.timeouts_after is not None and len(self.show_calls) > self.timeouts_after:
            raise LibbyTimeout(f"{name}: request timed out")
        return self._value(name)

    def _value(self, qualified: str) -> Dict[str, Any]:
        keyword = qualified.rsplit(".", 1)[-1]
        return self.values.get(keyword, {"ok": True, "value": 1.0, "units": "mm"})


class ResolveTests(unittest.TestCase):
    """Keyword selection and capability detection at resolve time."""

    def test_resolve_selects_and_detects_bulk_read(self):
        """Take names and the bulk-read capability from one keys.list."""
        collection = Collection(_collection_config())
        names = collection.resolve(_FakeClient())
        self.assertEqual(names, ("isconnected", "positionvalue"))
        self.assertTrue(collection.bulk_read)

    def test_peer_without_the_service_falls_back(self):
        """Read one keyword at a time when the peer omits keys.read."""
        client = _FakeClient(services=())
        collection = Collection(_collection_config())
        collection.resolve(client)
        self.assertFalse(collection.bulk_read)

        collection.tick(client, TIMESTAMP)
        self.assertEqual(client.read_calls, [])
        self.assertEqual(len(client.show_calls), 2)

    def test_resolve_is_due_before_the_first_read(self):
        """Resolve once before reading anything."""
        self.assertTrue(Collection(_collection_config()).needs_resolve())

    def test_resolve_is_not_due_again_until_refresh(self):
        """Hold the selection until the refresh window elapses."""
        clock = _FakeClock()
        collection = Collection(_collection_config(refresh_s=300.0,
                                                   interval_s=10.0),
                                clock=clock)
        collection.resolve(_FakeClient())
        self.assertFalse(collection.needs_resolve())

        clock.advance(299.0)
        self.assertFalse(collection.needs_resolve())
        clock.advance(2.0)
        self.assertTrue(collection.needs_resolve())


class TickTests(unittest.TestCase):
    """What one read of a collection produces."""

    def _ticked(self, client: _FakeClient):
        collection = Collection(_collection_config())
        collection.resolve(client)
        return collection.tick(client, TIMESTAMP)

    def test_one_request_covers_the_whole_tick(self):
        """Read a peer with a single bulk request."""
        client = _FakeClient()
        self._ticked(client)
        self.assertEqual(len(client.read_calls), 1)
        self.assertEqual(client.read_calls[0],
                         ("hsfei.adc.isconnected", "hsfei.adc.positionvalue"))

    def test_samples_carry_group_peer_units_and_timestamp(self):
        """Tag each sample with its own peer and the tick's read time."""
        result = self._ticked(_FakeClient())
        sample = next(s for s in result.samples if s.keyword == "positionvalue")
        self.assertEqual(sample.group, "hsfei")
        self.assertEqual(sample.peer, "adc")
        self.assertEqual(sample.units, "mm")
        self.assertEqual(sample.timestamp, TIMESTAMP)

    def test_every_sample_in_a_tick_shares_one_timestamp(self):
        """Stamp a tick once, so a dashboard can correlate its values."""
        result = self._ticked(_FakeClient())
        self.assertEqual({s.timestamp for s in result.samples}, {TIMESTAMP})

    def test_failed_read_is_counted_not_raised(self):
        """Report a broken getter as an error without losing the tick."""
        client = _FakeClient(values={
            "positionvalue": {"ok": False, "error": "hardware unreachable"},
        })
        result = self._ticked(client)
        self.assertEqual(result.read_errors, 1)
        self.assertEqual([s.keyword for s in result.samples], ["isconnected"])

    def test_null_value_is_kept_as_a_sample(self):
        """Leave a null for the sink to judge, so a backend may store it."""
        client = _FakeClient(values={
            "positionvalue": {"ok": True, "value": None},
        })
        result = self._ticked(client)
        self.assertEqual(result.read_errors, 0)
        sample = next(s for s in result.samples if s.keyword == "positionvalue")
        self.assertIsNone(sample.value)

    def test_tick_without_a_resolve_reads_nothing(self):
        """Do nothing until the selection is known."""
        client = _FakeClient()
        result = Collection(_collection_config()).tick(client, TIMESTAMP)
        self.assertEqual(result, type(result)((), 0))
        self.assertEqual(client.read_calls, [])

    def test_fallback_abandons_the_tick_after_a_timeout(self):
        """Stop reading a peer that has stopped answering.

        Paying timeout_s per keyword would overrun the interval many times
        over, so the rest of the tick is abandoned after the first timeout.
        """
        client = _FakeClient(
            names=("a", "b", "c", "d"), services=(), timeouts_after=1)
        collection = Collection(_collection_config())
        collection.resolve(client)
        result = collection.tick(client, TIMESTAMP)

        self.assertEqual(len(client.show_calls), 2)   # one ok, one timeout
        self.assertEqual(result.read_errors, 4 - 1)
        self.assertEqual(len(result.samples), 1)


if __name__ == "__main__":
    unittest.main()
