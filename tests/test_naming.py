"""Unit tests for keyword-name parsing and value coercion. No transport needed."""
import unittest

from libby.errors import KeywordNameError
from libby.naming import coerce_value, parse_keyword, peer_id


class ParseKeywordTests(unittest.TestCase):
    def test_splits_group_scope_name(self):
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

    def test_rejects_wildcard_in_group_or_scope(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hs%ei.pickoff.positionvalue")
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei.pick%ff.positionvalue")

    def test_rejects_wildcard_in_name_unless_allowed(self):
        with self.assertRaises(KeywordNameError):
            parse_keyword("hsfei.pickoff.is%")
        self.assertEqual(
            parse_keyword("hsfei.pickoff.is%", allow_pattern=True),
            ("hsfei", "pickoff", "is%"),
        )


class PeerIdTests(unittest.TestCase):
    def test_joins_with_underscore(self):
        # Matches every deployed daemon's peer_id (e.g. hsfei_pickoff), not a
        # dot: see plans/libby_lib_design.md.
        self.assertEqual(peer_id("hsfei", "pickoff"), "hsfei_pickoff")


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
