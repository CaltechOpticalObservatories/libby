"""Unit tests for DictKeyword and its registry builder. No transport needed."""
from __future__ import annotations

import unittest
from typing import Any, Dict, Optional

from libby import DictKeyword, KeywordRegistry


def _rw(initial: Optional[Dict[str, Any]] = None, **kwargs: Any) -> DictKeyword:
    """A read-write dict keyword backed by a one-slot store."""
    store = {"value": initial if initial is not None else {}}
    keyword = DictKeyword(
        "target",
        getter=lambda: store["value"],
        setter=lambda v: store.update(value=v),
        **kwargs,
    )
    keyword.store = store  # type: ignore[attr-defined]
    return keyword


class ModifyTests(unittest.TestCase):
    """What a modify accepts, rejects, and hands the setter."""

    def test_a_dict_is_applied_and_echoed(self):
        keyword = _rw()
        resp = keyword.handle({"value": {"x": 1.5, "y": -2, "label": "home"}})
        self.assertEqual(resp, {"ok": True, "value": {"x": 1.5, "y": -2, "label": "home"}})
        self.assertEqual(keyword.store["value"], {"x": 1.5, "y": -2, "label": "home"})

    def test_nested_values_pass_through(self):
        keyword = _rw()
        value = {"axes": {"x": 1.0}, "order": ["x", "y"], "fast": True}
        self.assertTrue(keyword.handle({"value": value})["ok"])
        self.assertEqual(keyword.store["value"], value)

    def test_an_empty_dict_is_accepted(self):
        self.assertTrue(_rw().handle({"value": {}})["ok"])

    def test_non_dicts_are_rejected(self):
        keyword = _rw()
        for bad in (True, 3, 2.5, "x=1", ["x", 1]):
            with self.subTest(bad=bad):
                self.assertEqual(keyword.handle({"value": bad}),
                                 {"ok": False, "error": "value must be a dict"})

    def test_non_string_keys_are_rejected(self):
        resp = _rw().handle({"value": {1: "a"}})
        self.assertEqual(resp, {"ok": False, "error": "dict keys must be strings"})

    def test_the_setter_gets_a_copy_of_the_payload(self):
        keyword = _rw()
        sent = {"x": 1}
        keyword.handle({"value": sent})
        sent["x"] = 99
        self.assertEqual(keyword.store["value"], {"x": 1})

    def test_null_is_rejected_unless_nullable(self):
        self.assertEqual(_rw().handle({"value": None}),
                         {"ok": False, "error": "value must be dict"})
        keyword = _rw(nullable=True)
        self.assertEqual(keyword.handle({"value": None}), {"ok": True, "value": None})
        self.assertIsNone(keyword.store["value"])

    def test_the_validator_sees_the_dict(self):
        def needs_xy(v: Dict[str, Any]) -> Optional[str]:
            missing = {"x", "y"} - v.keys()
            return f"missing {sorted(missing)}" if missing else None

        keyword = _rw(validator=needs_xy)
        self.assertEqual(keyword.handle({"value": {"x": 1}}),
                         {"ok": False, "error": "missing ['y']"})
        self.assertEqual(keyword.store["value"], {})
        self.assertTrue(keyword.handle({"value": {"x": 1, "y": 2}})["ok"])

    def test_write_only_takes_a_dict_as_a_parameterised_action(self):
        calls = []
        keyword = DictKeyword("move", setter=calls.append)
        self.assertTrue(keyword.handle({"value": {"x": 1.0, "speed": 5}})["ok"])
        self.assertEqual(calls, [{"x": 1.0, "speed": 5}])
        self.assertEqual(keyword.handle({}), {"ok": False, "error": "keyword is write-only"})


class ShowAndDescribeTests(unittest.TestCase):
    """Reading the value back, and the metadata a peer advertises."""

    def test_show_returns_the_getter_value(self):
        keyword = _rw({"x": 3})
        self.assertEqual(keyword.handle({}), {"ok": True, "value": {"x": 3}})

    def test_describe_reports_the_dict_type(self):
        meta = _rw(description="stage target").describe()
        self.assertEqual(meta["type"], "dict")
        self.assertEqual(meta["description"], "stage target")
        self.assertFalse(meta["readonly"])


class RegistryTests(unittest.TestCase):
    """The ``dict`` builder on KeywordRegistry."""

    def test_builds_and_buffers_a_dict_keyword(self):
        registry = KeywordRegistry()
        keyword = registry.dict("move", setter=lambda v: None, timeout_s=30.0)
        self.assertIsInstance(keyword, DictKeyword)
        self.assertEqual(keyword.timeout_s, 30.0)
        self.assertEqual(registry.drain(), [keyword])

    def test_the_builder_does_not_break_the_other_builders(self):
        # ``dict`` is defined on the class, shadowing the builtin in its body
        registry = KeywordRegistry()
        registry.bool("online", getter=lambda: True)
        registry.string("mode", getter=lambda: "idle")
        self.assertEqual([k.type_name for k in registry.drain()], ["bool", "string"])


if __name__ == "__main__":
    unittest.main()
