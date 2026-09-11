"""Unit tests for Client.wait_for's polling loop, against a fake transport.

No broker needed: the fake stands in for Libby and serves a scripted sequence
of values, so the loop's stop conditions can be checked deterministically.
"""
import unittest

from libby.client import Client
from libby.errors import ExpressionError, KeywordError, LibbyTimeout

KEYWORD = "hsfei.pickoff.positionvalue"


class _FakeLibby:
    """Serves a scripted sequence of values (or exceptions) over rpc()."""

    def __init__(self, values):
        self._values = list(values)
        self.calls = []

    def rpc(self, peer_id, key, payload, ttl_ms=8000):
        self.calls.append((peer_id, key, payload, ttl_ms))
        # Hold the last scripted value once the script runs out, so a wait
        # that polls more times than expected still terminates.
        value = self._values[min(len(self.calls), len(self._values)) - 1]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            return {"status": "delivered", "resp": value}
        return {"status": "delivered", "resp": {"ok": True, "value": value}}


def _client(values):
    return Client(_FakeLibby(values))


class WaitForTests(unittest.TestCase):
    def test_already_true_returns_immediately(self):
        client = _client([79.0])
        self.assertTrue(client.wait_for(f"${KEYWORD} > 15", 1.0, poll_s=0.01))
        self.assertEqual(len(client._libby.calls), 1)

    def test_becomes_true_after_a_few_polls(self):
        client = _client([1.0, 5.0, 20.0])
        result = client.wait_for_result(f"${KEYWORD} > 15", 2.0, poll_s=0.01)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.value, 20.0)
        self.assertEqual(result.polls, 3)
        self.assertEqual(result.keyword, KEYWORD)

    def test_timeout_returns_false_with_the_last_value_seen(self):
        client = _client([1.0])
        result = client.wait_for_result(f"${KEYWORD} > 15", 0.05, poll_s=0.01)
        self.assertFalse(result.satisfied)
        self.assertEqual(result.value, 1.0)
        self.assertGreaterEqual(result.elapsed_s, 0.05)
        self.assertGreaterEqual(result.polls, 1)

    def test_zero_timeout_evaluates_exactly_once(self):
        client = _client([1.0])
        self.assertFalse(client.wait_for(f"${KEYWORD} > 15", 0))
        self.assertEqual(len(client._libby.calls), 1)

    def test_addresses_the_peer_the_keyword_names(self):
        client = _client([79.0])
        client.wait_for(f"${KEYWORD} > 15", 1.0)
        peer, key, payload, _ttl = client._libby.calls[0]
        self.assertEqual(peer, "hsfei.pickoff")
        self.assertEqual(key, "positionvalue")
        self.assertEqual(payload, {})

    def test_default_service_addresses_the_same_peer(self):
        client = _client([79.0])
        client.wait_for("$positionvalue > 15", 1.0, service="hsfei.pickoff")
        peer, key, _payload, _ttl = client._libby.calls[0]
        self.assertEqual((peer, key), ("hsfei.pickoff", "positionvalue"))

    def test_rpc_timeout_applies_per_read_not_to_the_whole_wait(self):
        client = _client([79.0])
        client.wait_for(f"${KEYWORD} > 15", 600.0, rpc_timeout_s=2.0)
        self.assertEqual(client._libby.calls[0][3], 2000)

    def test_strings_compare_case_insensitively_by_default(self):
        client = _client(["READY"])
        self.assertTrue(client.wait_for("$hsfei.pickoff.status == Ready", 1.0))

    def test_case_true_keeps_waiting_on_a_case_mismatch(self):
        client = _client(["READY"])
        self.assertFalse(
            client.wait_for("$hsfei.pickoff.status == Ready", 0.05,
                            case=True, poll_s=0.01)
        )

    def test_daemon_rejection_raises_rather_than_waiting_it_out(self):
        # ok=False means unknown/write-only keyword: waiting can't fix it.
        client = _client([{"ok": False, "error": "unknown keyword 'nope'"}])
        with self.assertRaises(KeywordError):
            client.wait_for("$hsfei.pickoff.nope > 15", 1.0)
        self.assertEqual(len(client._libby.calls), 1)

    def test_transient_rpc_timeout_is_retried(self):
        client = _client([LibbyTimeout("no response"), 79.0])
        result = client.wait_for_result(f"${KEYWORD} > 15", 2.0, poll_s=0.01)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.polls, 2)

    def test_unreachable_peer_times_out_rather_than_raising(self):
        client = _client([LibbyTimeout("no response")])
        result = client.wait_for_result(f"${KEYWORD} > 15", 0.05, poll_s=0.01)
        self.assertFalse(result.satisfied)
        self.assertIsNone(result.value)

    def test_malformed_expression_raises_before_any_rpc(self):
        client = _client([79.0])
        with self.assertRaises(ExpressionError):
            client.wait_for("positionvalue > 15", 1.0)
        self.assertEqual(client._libby.calls, [])

    def test_uncomparable_value_raises_rather_than_waiting_it_out(self):
        client = _client(["ready"])
        with self.assertRaises(ExpressionError):
            client.wait_for(f"${KEYWORD} > 15", 1.0)

    def test_null_value_keeps_waiting_then_succeeds(self):
        # A nullable keyword not yet populated is 'not true yet', not an error.
        client = _client([None, None, 79.0])
        result = client.wait_for_result(f"${KEYWORD} > 15", 2.0, poll_s=0.01)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.polls, 3)


if __name__ == "__main__":
    unittest.main()
