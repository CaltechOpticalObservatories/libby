"""Unit tests for keyword-name parsing and value coercion. No transport needed."""
import unittest

from libby.errors import KeywordNameError
from libby.naming import (
    coerce_value,
    parse_address_pattern,
    parse_keyword,
    peer_id,
    qualified_peer_id,
)


class ParseKeywordTests(unittest.TestCase):
    def test_splits_group_daemon_keyword(self):
        self.assertEqual(
            parse_keyword("hsfei.pickoff.positionvalue"),
            ("hsfei", "pickoff", "positionvalue"),
        )

    def test_rejects_too_few_segments(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei.pickoff")

    def test_rejects_empty_segment(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei..positionvalue")

    def test_rejects_wildcard_in_group_or_daemon(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hs%ei.pickoff.positionvalue")
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei.pick%ff.positionvalue")

    def test_rejects_wildcard_in_keyword_unless_allowed(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei.pickoff.is%")
        self.assertEqual(
            parse_keyword("hsfei.pickoff.is%", allow_pattern=True),
            ("hsfei", "pickoff", "is%"),
        )


class ParseAddressPatternTests(unittest.TestCase):
    def test_two_segments_mean_no_keyword(self):
        address = parse_address_pattern("hsfei.%")
        self.assertEqual((address.group, address.daemon, address.keyword), ("hsfei", "%", None))

    def test_wildcard_daemon_spans_peers(self):
        address = parse_address_pattern("HSFEI.%.is%")
        self.assertTrue(address.spans_peers)
        self.assertEqual(address.keyword, "is%")
        # Wire ids are lowercased, so the pattern matched against them is too
        self.assertEqual(address.peer_pattern, "hsfei.%")

    def test_exact_daemon_does_not_span_peers(self):
        self.assertFalse(parse_address_pattern("hsfei.pickoff.is%").spans_peers)

    def test_rejects_one_segment_and_empty_segments(self):
        with self.assertRaises(KeywordNameError):
            parse_address_pattern("hsfei")
        with self.assertRaises(KeywordNameError):
            parse_address_pattern("hsfei..positionvalue")


class PeerIdTests(unittest.TestCase):
    def test_joins_group_and_daemon_with_a_dot(self):
        # Must match the wire identity qualified_peer_id builds daemon-side
        self.assertEqual(peer_id("hsfei", "adc"), "hsfei.adc")

    def test_case_insensitive(self):
        # A daemon configured with mixed/upper case and a client addressing
        # it in a different case must agree on the same wire identity.
        self.assertEqual(peer_id("HSFEI", "ADC"), peer_id("hsfei", "adc"))
        self.assertEqual(peer_id("HsFei", "AdC"), "hsfei.adc")


class QualifiedPeerIdTests(unittest.TestCase):
    def test_no_group_id_returns_lowercased_peer_id(self):
        self.assertEqual(qualified_peer_id("ADC"), "adc")

    def test_joins_and_lowercases_both(self):
        self.assertEqual(qualified_peer_id("ADC", "HSFEI"), "hsfei.adc")

    def test_empty_group_id_treated_as_none(self):
        self.assertEqual(qualified_peer_id("ADC", ""), "adc")


class CoerceValueTests(unittest.TestCase):
    def test_empty_and_null_become_none(self):
        self.assertIsNone(coerce_value(""))
        self.assertIsNone(coerce_value("null"))
        self.assertIsNone(coerce_value("NULL"))

    def test_bool_literals(self):
        self.assertIs(coerce_value("true"), True)
        self.assertIs(coerce_value("False"), False)

    def test_numeric_coercion(self):
        self.assertEqual(coerce_value("7"), 7)
        self.assertIsInstance(coerce_value("7"), int)
        self.assertEqual(coerce_value("3.5"), 3.5)
        self.assertIsInstance(coerce_value("3.5"), float)

    def test_falls_back_to_string(self):
        self.assertEqual(coerce_value("engineering"), "engineering")


if __name__ == "__main__":
    unittest.main()
