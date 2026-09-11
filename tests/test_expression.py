"""Unit tests for wait_for expression parsing and evaluation. No transport needed."""
import unittest

from libby.errors import ExpressionError, KeywordNameError
from libby.expression import Comparison, parse_comparison


class ParseComparisonTests(unittest.TestCase):
    def test_parses_keyword_operator_literal(self):
        self.assertEqual(
            parse_comparison("$hsfei.pickoff.positionvalue > 15"),
            Comparison("hsfei.pickoff.positionvalue", ">", 15),
        )

    def test_accepts_a_parenthesized_comparison(self):
        self.assertEqual(
            parse_comparison("($hsfei.pickoff.positionvalue > 15)"),
            Comparison("hsfei.pickoff.positionvalue", ">", 15),
        )

    def test_strips_nested_redundant_parens(self):
        self.assertEqual(
            parse_comparison("(($hsfei.pickoff.softmax >= 120))"),
            Comparison("hsfei.pickoff.softmax", ">=", 120),
        )

    def test_parens_around_only_the_keyword_are_stripped(self):
        self.assertEqual(
            parse_comparison("($hsfei.pickoff.softmax) >= 120"),
            Comparison("hsfei.pickoff.softmax", ">=", 120),
        )

    def test_quoting_keeps_parens_that_belong_to_the_value(self):
        self.assertEqual(
            parse_comparison("$hsfei.pickoff.status == '(Ready)'"),
            Comparison("hsfei.pickoff.status", "==", "(Ready)"),
        )

    def test_separately_parenthesized_operands_are_not_unwrapped_as_one(self):
        # '(a) == (b)' must not collapse to 'a) == (b'; the leading paren
        # closes before the end, so the outer pair isn't a wrapper.
        with self.assertRaises(ExpressionError):
            parse_comparison("(3) == (4)")

    def test_tolerates_missing_whitespace(self):
        self.assertEqual(
            parse_comparison("$hsfei.pickoff.softmax>=120"),
            Comparison("hsfei.pickoff.softmax", ">=", 120),
        )

    def test_all_six_operators_parse(self):
        for op in ("==", "!=", "<", "<=", ">", ">="):
            self.assertEqual(
                parse_comparison(f"$hsfei.pickoff.softmax {op} 5").op, op
            )

    def test_keyword_on_the_right_flips_the_operator(self):
        # '15 < $foo' means the same as '$foo > 15'; store it keyword-first.
        self.assertEqual(
            parse_comparison("15 < $hsfei.pickoff.positionvalue"),
            Comparison("hsfei.pickoff.positionvalue", ">", 15),
        )
        self.assertEqual(
            parse_comparison("5 != $hsfei.pickoff.softmax"),
            Comparison("hsfei.pickoff.softmax", "!=", 5),
        )

    def test_bare_name_resolves_against_a_default_service(self):
        self.assertEqual(
            parse_comparison("$ismoving == false", service="hsfei.pickoff"),
            Comparison("hsfei.pickoff.ismoving", "==", False),
        )

    def test_qualified_name_ignores_the_default_service(self):
        self.assertEqual(
            parse_comparison("$hsfei.adc.ismoving == false",
                             service="hsfei.pickoff").keyword,
            "hsfei.adc.ismoving",
        )

    def test_bare_name_without_a_service_is_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("$ismoving == false")

    def test_two_segment_reference_is_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("$pickoff.ismoving == false")

    def test_malformed_service_is_rejected(self):
        for service in ("hsfei", "hsfei.pickoff.extra", "hsfei."):
            with self.assertRaises(ExpressionError):
                parse_comparison("$ismoving == false", service=service)

    def test_wildcard_in_keyword_is_rejected(self):
        with self.assertRaises(KeywordNameError):
            parse_comparison("$hsfei.pickoff.is% == true")

    def test_missing_operator_is_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("$hsfei.pickoff.ismoving")

    def test_single_equals_names_the_fix(self):
        with self.assertRaises(ExpressionError) as caught:
            parse_comparison("$hsfei.pickoff.softmax = 5")
        self.assertIn("'=='", str(caught.exception))

    def test_no_keyword_reference_is_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("3 < 4")

    def test_two_keyword_references_are_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("$hsfei.pickoff.softmin < $hsfei.pickoff.softmax")

    def test_empty_side_is_rejected(self):
        with self.assertRaises(ExpressionError):
            parse_comparison("$hsfei.pickoff.softmax >")

    def test_empty_expression_is_rejected(self):
        for expression in ("", "   ", None):
            with self.assertRaises(ExpressionError):
                parse_comparison(expression)


class LiteralCoercionTests(unittest.TestCase):
    def _operand(self, text, **kwargs):
        return parse_comparison(f"$hsfei.pickoff.k == {text}", **kwargs).operand

    def test_unquoted_values_coerce_like_a_modify_value(self):
        self.assertEqual(self._operand("15"), 15)
        self.assertEqual(self._operand("15.5"), 15.5)
        self.assertEqual(self._operand("-5"), -5)
        self.assertIs(self._operand("true"), True)
        self.assertIs(self._operand("false"), False)
        self.assertIsNone(self._operand("null"))
        self.assertEqual(self._operand("Ready"), "Ready")

    def test_quoting_keeps_a_value_a_string(self):
        self.assertEqual(self._operand("'15'"), "15")
        self.assertEqual(self._operand("'false'"), "false")
        self.assertEqual(self._operand('"Ready"'), "Ready")

    def test_only_one_level_of_quoting_is_stripped(self):
        self.assertEqual(self._operand('"\'Ready\'"'), "'Ready'")

    def test_triple_quotes_strip_as_one_level(self):
        self.assertEqual(self._operand("'''Ready'''"), "Ready")
        self.assertEqual(self._operand('"""Ready"""'), "Ready")

    def test_quoted_value_may_contain_whitespace(self):
        self.assertEqual(self._operand("'not ready'"), "not ready")

    def test_quoted_value_may_contain_an_operator(self):
        # The operator scanner has to skip quoted runs to get this right.
        self.assertEqual(
            parse_comparison("$hsfei.pickoff.status != '>= 5'"),
            Comparison("hsfei.pickoff.status", "!=", ">= 5"),
        )

    def test_quoted_value_may_start_with_a_dollar_sign(self):
        self.assertEqual(self._operand("'$notakeyword'"), "$notakeyword")


class EvaluateTests(unittest.TestCase):
    def _eval(self, expression, value, **kwargs):
        return parse_comparison(expression).evaluate(value, **kwargs)

    def test_ordering_operators(self):
        expression = "$hsfei.pickoff.positionvalue > 15"
        self.assertTrue(self._eval(expression, 15.5))
        self.assertFalse(self._eval(expression, 15))
        self.assertFalse(self._eval(expression, 14.9))

    def test_int_operand_compares_against_a_float_value(self):
        self.assertTrue(self._eval("$hsfei.pickoff.positionvalue >= 15", 79.0))

    def test_equality_on_bools(self):
        expression = "$hsfei.pickoff.ismoving == false"
        self.assertTrue(self._eval(expression, False))
        self.assertFalse(self._eval(expression, True))

    def test_equality_on_null(self):
        expression = "$hsfei.pickoff.softmax == null"
        self.assertTrue(self._eval(expression, None))
        self.assertFalse(self._eval(expression, 120.0))

    def test_strings_compare_case_insensitively_by_default(self):
        expression = "$hsfei.pickoff.status == Ready"
        self.assertTrue(self._eval(expression, "ready"))
        self.assertTrue(self._eval(expression, "READY"))

    def test_case_true_compares_strings_exactly(self):
        expression = "$hsfei.pickoff.status == Ready"
        self.assertFalse(self._eval(expression, "ready", case=True))
        self.assertTrue(self._eval(expression, "Ready", case=True))

    def test_null_value_under_an_ordering_operator_is_unsatisfied(self):
        # A keyword with no value yet is 'not true yet', so a wait started
        # before a nullable keyword is populated keeps waiting.
        self.assertFalse(self._eval("$hsfei.pickoff.softmax > 15", None))
        self.assertFalse(self._eval("$hsfei.pickoff.softmax < 15", None))

    def test_uncomparable_types_raise(self):
        with self.assertRaises(ExpressionError):
            self._eval("$hsfei.pickoff.status > 15", "ready")

    def test_str_repr_shows_the_normalized_comparison(self):
        self.assertEqual(
            str(parse_comparison("15 < $hsfei.pickoff.positionvalue")),
            "$hsfei.pickoff.positionvalue > 15",
        )


if __name__ == "__main__":
    unittest.main()
