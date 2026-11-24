"""Unit tests for DictKeyword and its registry builder. No transport needed."""
from __future__ import annotations

import unittest
from typing import Any, Dict, Optional

from libby import (BoolKeyword, DictKeyword, Field, FloatKeyword, IntKeyword,
                   KeywordRegistry, StringKeyword)
from libby.keyword import FIELD_TYPES

MOVE_FIELDS = {
    "x": "float",
    "y": "float",
    "speed": Field("float", units="mm/s", required=False, description="Slew rate"),
}


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


class SchemaTests(unittest.TestCase):
    """A ``fields`` schema: which keys are allowed, required, and of what type."""

    def test_a_conforming_dict_is_applied_with_floats_cast(self):
        keyword = _rw(fields=MOVE_FIELDS)
        resp = keyword.handle({"value": {"x": 1, "y": 2.5}})
        self.assertEqual(resp, {"ok": True, "value": {"x": 1.0, "y": 2.5}})
        self.assertIsInstance(keyword.store["value"]["x"], float)

    def test_an_optional_field_left_out_stays_absent(self):
        keyword = _rw(fields=MOVE_FIELDS)
        keyword.handle({"value": {"x": 1.0, "y": 2.0}})
        self.assertNotIn("speed", keyword.store["value"])

    def test_an_optional_field_is_checked_when_sent(self):
        resp = _rw(fields=MOVE_FIELDS).handle({"value": {"x": 1, "y": 2, "speed": "fast"}})
        self.assertEqual(resp, {"ok": False, "error": "field 'speed': value must be a number"})

    def test_unknown_fields_are_named_with_the_expected_ones(self):
        keyword = _rw(fields=MOVE_FIELDS)
        self.assertEqual(
            keyword.handle({"value": {"x": 1, "y": 2, "z": 3}}),
            {"ok": False, "error": "unknown field 'z'; expected 'x', 'y', 'speed'"})
        self.assertEqual(
            keyword.handle({"value": {"x": 1, "y": 2, "z": 3, "w": 4}})["error"],
            "unknown fields 'z', 'w'; expected 'x', 'y', 'speed'")
        self.assertEqual(keyword.store["value"], {})

    def test_missing_required_fields_are_named(self):
        keyword = _rw(fields=MOVE_FIELDS)
        self.assertEqual(keyword.handle({"value": {"x": 1}}),
                         {"ok": False, "error": "missing required field 'y'"})
        self.assertEqual(keyword.handle({"value": {}})["error"],
                         "missing required fields 'x', 'y'")

    def test_each_field_type_uses_the_scalar_keyword_rules(self):
        keyword = _rw(fields={"b": Field("bool", required=False),
                              "i": Field("int", required=False),
                              "f": Field("float", required=False),
                              "s": Field("string", required=False)})
        cases = [
            ("b", 1, "value must be a bool"),
            ("i", True, "value must be an int"),
            ("i", 1.5, "value must be an int"),
            ("f", False, "value must be a number"),
            ("s", 3, "value must be a string"),
        ]
        for field, bad, message in cases:
            with self.subTest(field=field, bad=bad):
                self.assertEqual(keyword.handle({"value": {field: bad}}),
                                 {"ok": False, "error": f"field '{field}': {message}"})
        good = {"b": True, "i": 3, "f": 2, "s": "ok"}
        self.assertEqual(keyword.handle({"value": good})["value"],
                         {"b": True, "i": 3, "f": 2.0, "s": "ok"})

    def test_any_fields_are_not_checked(self):
        keyword = _rw(fields={"extra": "any"})
        value = {"extra": {"nested": [1, "two"]}}
        self.assertEqual(keyword.handle({"value": value}), {"ok": True, "value": value})

    def test_null_field_needs_a_nullable_field(self):
        keyword = _rw(fields={"label": "string",
                              "note": Field("string", nullable=True)})
        self.assertEqual(keyword.handle({"value": {"label": None, "note": "x"}}),
                         {"ok": False, "error": "field 'label': value must not be null"})
        resp = keyword.handle({"value": {"label": "home", "note": None}})
        self.assertEqual(resp, {"ok": True, "value": {"label": "home", "note": None}})

    def test_a_nullable_field_is_still_required(self):
        keyword = _rw(fields={"note": Field("string", nullable=True)})
        self.assertEqual(keyword.handle({"value": {}})["error"],
                         "missing required field 'note'")

    def test_the_validator_runs_after_the_schema(self):
        seen = []

        def validator(v: Dict[str, Any]) -> Optional[str]:
            seen.append(v)
            return "x must be below y" if v["x"] >= v["y"] else None

        keyword = _rw(fields=MOVE_FIELDS, validator=validator)
        self.assertEqual(keyword.handle({"value": {"x": 1}})["error"],
                         "missing required field 'y'")
        self.assertEqual(seen, [])
        self.assertEqual(keyword.handle({"value": {"x": 5, "y": 2}})["error"],
                         "x must be below y")
        self.assertEqual(seen, [{"x": 5.0, "y": 2.0}])

    def test_an_empty_schema_accepts_only_an_empty_dict(self):
        keyword = _rw(fields={})
        self.assertTrue(keyword.handle({"value": {}})["ok"])
        self.assertFalse(keyword.handle({"value": {"x": 1}})["ok"])


class SchemaConstructionTests(unittest.TestCase):
    """A bad schema fails when the keyword is built, not on the first modify."""

    def test_an_unknown_type_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "keyword 'target': field 'x': unknown field type 'double'"):
            _rw(fields={"x": "double"})

    def test_an_unknown_field_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown field type 'dict'"):
            Field("dict")

    def test_a_spec_must_be_a_name_or_a_field(self):
        with self.assertRaisesRegex(TypeError, "field 'x' must be a type name or a Field"):
            _rw(fields={"x": float})

    def test_field_names_must_be_non_empty_strings(self):
        for bad in ("", 1):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(ValueError, "field names must be non-empty strings"):
                    _rw(fields={bad: "float"})

    def test_the_advertised_field_types(self):
        self.assertEqual(FIELD_TYPES, ("bool", "int", "float", "string", "any"))


class ScalarCheckTests(unittest.TestCase):
    """Field.check mirrors the scalar types' checks; pin the scalar side so the two
    cannot drift (SchemaTests pins the field side)."""

    def test_scalar_keywords_keep_their_messages(self):
        cases = [
            (BoolKeyword, 1, "value must be a bool"),
            (IntKeyword, True, "value must be an int"),
            (FloatKeyword, "1", "value must be a number"),
            (StringKeyword, 1, "value must be a string"),
        ]
        for cls, bad, message in cases:
            with self.subTest(cls=cls.__name__):
                keyword = cls("k", setter=lambda v: None)
                self.assertEqual(keyword.handle({"value": bad}),
                                 {"ok": False, "error": message})

    def test_float_keyword_still_casts_ints(self):
        self.assertEqual(FloatKeyword("k", setter=lambda v: None).handle({"value": 2}),
                         {"ok": True, "value": 2.0})


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

    def test_describe_omits_fields_without_a_schema(self):
        self.assertNotIn("fields", _rw().describe())

    def test_describe_advertises_the_schema_in_order(self):
        meta = _rw(fields=MOVE_FIELDS).describe()
        self.assertEqual(meta["fields"], {
            "x": {"type": "float", "required": True, "nullable": False},
            "y": {"type": "float", "required": True, "nullable": False},
            "speed": {"type": "float", "required": False, "nullable": False,
                      "units": "mm/s", "description": "Slew rate"},
        })
        self.assertEqual(list(meta["fields"]), ["x", "y", "speed"])


class RegistryTests(unittest.TestCase):
    """The ``dict`` builder on KeywordRegistry."""

    def test_builds_and_buffers_a_dict_keyword(self):
        registry = KeywordRegistry()
        keyword = registry.dict("move", setter=lambda v: None, timeout_s=30.0)
        self.assertIsInstance(keyword, DictKeyword)
        self.assertEqual(keyword.timeout_s, 30.0)
        self.assertEqual(registry.drain(), [keyword])

    def test_the_builder_takes_a_schema(self):
        keyword = KeywordRegistry().dict("move", setter=lambda v: None, fields=MOVE_FIELDS)
        self.assertEqual(list(keyword.describe()["fields"]), ["x", "y", "speed"])
        self.assertFalse(keyword.handle({"value": {"x": 1}})["ok"])

    def test_the_builder_does_not_break_the_other_builders(self):
        # ``dict`` is defined on the class, shadowing the builtin in its body
        registry = KeywordRegistry()
        registry.bool("online", getter=lambda: True)
        registry.string("mode", getter=lambda: "idle")
        self.assertEqual([k.type_name for k in registry.drain()], ["bool", "string"])


if __name__ == "__main__":
    unittest.main()
