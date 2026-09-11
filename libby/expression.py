"""Expressions for ``wait_for``: one keyword compared against one literal.

::

    $hsfei.pickoff.positionvalue > 15
    $hsfei.pickoff.ismoving == false
    $hsfei.pickoff.status != Ready

Keyword references are prefixed with ``$``, and are either fully qualified
(``$<group>.<scope>.<name>``) or, when a default service is supplied, a bare
name (``$<name>``).

Values need quoting only when they contain whitespace, start with ``$``, or
look like an operator; one level of quoting is stripped. An unquoted value is
coerced the way the CLI coerces a ``modify`` value, so ``false`` is a bool and
``15`` an int, while ``'false'`` is the string.

Boolean operators, arithmetic, and multi-keyword expressions are not
supported.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Tuple

from .errors import ExpressionError
from .naming import coerce_value, parse_keyword

# Longest-first: '==' must win over '=', '<=' over '<'.
_OPERATORS = ("==", "!=", "<=", ">=", "<", ">")

# Longest-first again, so '''triple''' is not read as ''+'triple'+''.
_QUOTES = ("'''", '"""', "'", '"')

# Applied when the keyword is on the right: '15 < $foo' becomes '$foo > 15'.
_FLIPPED = {"==": "==", "!=": "!=", "<": ">", ">": "<", "<=": ">=", ">=": "<="}

_ORDERING = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


@dataclass(frozen=True)
class Comparison:
    """One parsed comparison: a keyword, an operator, and a literal.

    Always keyword-first, so a caller reads one keyword and compares one way;
    :func:`parse_comparison` flips the operator to make it so.
    """

    keyword: str
    """Qualified ``<group>.<scope>.<name>`` of the keyword to read."""

    op: str
    """One of ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``."""

    operand: Any
    """The literal the keyword's value is compared against."""

    def evaluate(self, value: Any, *, case: bool = False) -> bool:
        """Compare ``value`` (a keyword's current value) against the operand.

        String comparisons are case-insensitive unless ``case`` is true.

        A ``value`` of ``None`` under an ordering operator is unsatisfied
        rather than an error, so a wait started before a daemon has populated
        a nullable keyword keeps waiting instead of failing.

        Raises:
            ExpressionError: the two types cannot be ordered at all (e.g.
                ``"ready" > 15``), which no amount of waiting will fix.
        """
        left, right = value, self.operand
        if not case and isinstance(left, str) and isinstance(right, str):
            left, right = left.casefold(), right.casefold()
        if self.op == "==":
            return left == right
        if self.op == "!=":
            return left != right
        if left is None:
            return False
        try:
            return bool(_ORDERING[self.op](left, right))
        except TypeError as ex:
            raise ExpressionError(
                f"cannot compare {type(value).__name__} {value!r} "
                f"{self.op} {type(self.operand).__name__} {self.operand!r}: {ex}"
            ) from ex

    def __str__(self) -> str:
        return f"${self.keyword} {self.op} {self.operand!r}"


def parse_comparison(
    expression: str,
    *,
    service: Optional[str] = None,
) -> Comparison:
    """Parse ``expression`` into a :class:`Comparison`.

    Args:
        expression: One comparison, e.g. ``'$hsfei.pickoff.softmax >= 120'``.
        service: Optional default ``<group>.<scope>``, letting the expression
            name a keyword bare (``'$softmax >= 120'``).

    Raises:
        ExpressionError: the expression is not a single keyword-to-literal
            comparison, or the default service is malformed.
        KeywordNameError: the resolved keyword name is not a valid
            ``<group>.<scope>.<name>``.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("expression must be a non-empty string")
    if service is not None:
        service = _check_service(service)

    left_text, op, right_text = _split_on_operator(
        _strip_outer_parens(expression.strip())
    )
    left_text = _strip_outer_parens(left_text.strip())
    right_text = _strip_outer_parens(right_text.strip())
    if not left_text or not right_text:
        raise ExpressionError(
            f"expression needs a value on both sides of {op!r}: {expression}"
        )

    left_is_keyword = left_text.startswith("$")
    right_is_keyword = right_text.startswith("$")
    if left_is_keyword and right_is_keyword:
        raise ExpressionError(
            "comparing two keywords is not supported; compare one keyword "
            f"against a literal: {expression}"
        )
    if not (left_is_keyword or right_is_keyword):
        raise ExpressionError(
            "expression must reference exactly one keyword, prefixed with "
            f"'$': {expression}"
        )

    if left_is_keyword:
        return Comparison(
            keyword=_parse_keyword_ref(left_text, service),
            op=op,
            operand=_parse_literal(right_text),
        )
    return Comparison(
        keyword=_parse_keyword_ref(right_text, service),
        op=_FLIPPED[op],
        operand=_parse_literal(left_text),
    )


def _unquoted_indices(text: str) -> Iterator[int]:
    """Yield each index of ``text`` that sits outside a quoted run.

    Skipping quoted runs lets a value carry a structural character, as in
    ``$foo.bar != '>= 5'``.
    """
    index = 0
    quote: Optional[str] = None
    while index < len(text):
        if quote is not None:
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
            else:
                index += 1
            continue
        opening = next((q for q in _QUOTES if text.startswith(q, index)), None)
        if opening is not None:
            quote = opening
            index += len(opening)
            continue
        yield index
        index += 1


def _strip_outer_parens(text: str) -> str:
    """Drop redundant wrapping parentheses.

    A lone comparison doesn't need them, but ``($foo.bar.baz > 15)`` is a
    natural way to write one. Quote a value to keep parens that belong to it:
    ``'(Ready)'``.
    """
    while len(text) > 1 and text.startswith("(") and text.endswith(")"):
        if not _parens_wrap_all(text):
            break
        text = text[1:-1].strip()
    return text


def _parens_wrap_all(text: str) -> bool:
    """True if ``text``'s leading '(' is closed only by its trailing ')'.

    Keeps ``(a) == (b)``, where the first pair closes early, from being
    unwrapped as though it were one parenthesized whole.
    """
    depth = 0
    last = len(text) - 1
    for index in _unquoted_indices(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth <= 0:
                return depth == 0 and index == last
    return False


def _split_on_operator(text: str) -> Tuple[str, str, str]:
    """Split ``text`` at its first comparison operator, ignoring quoted runs."""
    for index in _unquoted_indices(text):
        found = next((o for o in _OPERATORS if text.startswith(o, index)), None)
        if found is not None:
            return text[:index], found, text[index + len(found):]

    if "=" in text:
        raise ExpressionError(
            f"'=' is assignment, not comparison; use '==': {text}"
        )
    raise ExpressionError(
        "expression must contain one of ==, !=, <, <=, >, >=: " + text
    )


def _check_service(service: str) -> str:
    """Validate a default service is exactly ``<group>.<scope>``."""
    parts = service.split(".")
    if len(parts) != 2 or not all(parts):
        raise ExpressionError(
            f"service must be <group>.<scope>, got: {service}"
        )
    return service


def _parse_keyword_ref(text: str, service: Optional[str]) -> str:
    """Resolve a ``$``-prefixed reference to a qualified keyword name."""
    ref = text[1:]
    if not ref:
        raise ExpressionError("expected a keyword name after '$'")
    dots = ref.count(".")
    if dots == 2:
        qualified = ref
    elif dots == 0:
        if not service:
            raise ExpressionError(
                f"'${ref}' is not qualified: write "
                f"'$<group>.<scope>.{ref}', or supply a default service"
            )
        qualified = f"{service}.{ref}"
    else:
        raise ExpressionError(
            f"keyword reference must be '$<group>.<scope>.<name>' or, with a "
            f"default service, '$<name>': ${ref}"
        )
    parse_keyword(qualified)
    return qualified


def _parse_literal(text: str) -> Any:
    """Strip one level of quoting, or coerce an unquoted value.

    Quoted values stay strings; unquoted ones coerce like a CLI ``modify``
    value, so ``15`` is an int and ``null`` is ``None``.
    """
    for quote in _QUOTES:
        if (
            len(text) >= 2 * len(quote)
            and text.startswith(quote)
            and text.endswith(quote)
        ):
            return text[len(quote):-len(quote)]
    return coerce_value(text)


__all__ = ["Comparison", "parse_comparison"]
