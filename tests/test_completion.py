"""Unit tests for the CLI's address completion and its cache. No transport needed."""
import tempfile
import time
import unittest
from pathlib import Path

from libby.cli.completion import (
    CompletionCache,
    address_candidates,
    cached_listings,
    list_candidates,
    peer_candidates,
)

LISTINGS = {
    "hsfei.adc": ["isconnected", "position"],
    "hsfei.atcfw": ["isconnected"],
    "hscal.hkettherm": ["t2A_red_etalon"],
}
CONNECTION = "rabbitmq amqp://localhost"


class AddressCandidatesTests(unittest.TestCase):
    def test_partial_group_offers_daemons_with_a_trailing_dot(self):
        self.assertEqual(address_candidates("hs", LISTINGS),
                         ["hscal.hkettherm.", "hsfei.adc.", "hsfei.atcfw."])

    def test_unambiguous_daemon_expands_to_its_keywords(self):
        # Never a lone "<group>.<daemon>." candidate, which the shell would
        # follow with a space
        self.assertEqual(address_candidates("hsfei.at", LISTINGS),
                         ["hsfei.atcfw.isconnected"])

    def test_complete_daemon_offers_its_keywords(self):
        self.assertEqual(address_candidates("hsfei.adc.", LISTINGS),
                         ["hsfei.adc.isconnected", "hsfei.adc.position"])

    def test_daemon_is_case_insensitive_and_keyword_is_not(self):
        self.assertEqual(address_candidates("HSFEI.adc.pos", LISTINGS), ["hsfei.adc.position"])
        self.assertEqual(address_candidates("hscal.hkettherm.T", LISTINGS), [])

    def test_unknown_daemon_offers_nothing(self):
        self.assertEqual(address_candidates("hsfei.nosuch.", LISTINGS), [])


class ListCandidatesTests(unittest.TestCase):
    """Completing a list pattern, where a daemon may stand alone."""

    def test_empty_prefix_offers_wildcards_and_bare_peers(self):
        self.assertEqual(list_candidates("", LISTINGS),
                         ["%.%", "hscal.%", "hscal.hkettherm",
                          "hsfei.%", "hsfei.adc", "hsfei.atcfw"])

    def test_candidates_never_fail_to_match_the_prefix(self):
        # "%.%" must not be offered once a literal group is typed
        for candidate in list_candidates("hs", LISTINGS):
            self.assertTrue(candidate.startswith("hs"), candidate)

    def test_group_prefix_offers_its_wildcard_and_daemons(self):
        self.assertEqual(list_candidates("hsfei.", LISTINGS),
                         ["hsfei.%", "hsfei.adc", "hsfei.atcfw"])

    def test_daemon_prefix_offers_its_keywords_and_wildcard(self):
        self.assertEqual(list_candidates("hsfei.adc.", LISTINGS),
                         ["hsfei.adc.%", "hsfei.adc.isconnected", "hsfei.adc.position"])

    def test_wildcard_daemon_spans_the_group(self):
        self.assertEqual(list_candidates("hsfei.%.", LISTINGS),
                         ["hsfei.%.%", "hsfei.%.isconnected", "hsfei.%.position"])

    def test_partial_keyword_narrows_and_drops_the_wildcard(self):
        self.assertEqual(list_candidates("hsfei.adc.is", LISTINGS),
                         ["hsfei.adc.isconnected"])


class PeerCandidatesTests(unittest.TestCase):
    def test_offers_matching_daemons_without_a_trailing_dot(self):
        self.assertEqual(peer_candidates("hsfei.", LISTINGS), ["hsfei.adc", "hsfei.atcfw"])

    def test_empty_prefix_offers_everything(self):
        self.assertEqual(len(peer_candidates("", LISTINGS)), len(LISTINGS))


class CompletionCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "cache.json"

    def test_missing_file_is_a_miss(self):
        self.assertIsNone(CompletionCache(self.path).load(CONNECTION))

    def test_store_then_load_round_trips_and_creates_the_directory(self):
        cache = CompletionCache(self.path)
        cache.store(CONNECTION, LISTINGS)
        self.assertEqual(cache.load(CONNECTION), LISTINGS)

    def test_another_connection_is_a_miss(self):
        cache = CompletionCache(self.path)
        cache.store(CONNECTION, LISTINGS)
        self.assertIsNone(cache.load("zmq []"))

    def test_expired_entry_is_a_miss(self):
        cache = CompletionCache(self.path, ttl_s=0.0)
        cache.store(CONNECTION, LISTINGS)
        time.sleep(0.01)
        self.assertIsNone(cache.load(CONNECTION))

    def test_corrupt_file_is_a_miss(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(CompletionCache(self.path).load(CONNECTION))

    def test_cached_listings_fetches_once_within_the_ttl(self):
        calls = []

        def fetch(timeout_s: float):
            calls.append(timeout_s)
            return LISTINGS

        cache = CompletionCache(self.path)
        self.assertEqual(cached_listings(CONNECTION, fetch, cache), LISTINGS)
        self.assertEqual(cached_listings(CONNECTION, fetch, cache), LISTINGS)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
