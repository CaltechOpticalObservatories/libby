"""Unit tests for dict values through ``libby modify`` and ``libby show``. No transport needed."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Tuple
from unittest import mock

from libby import DictKeyword, Field
from libby.cli import libby_cli


class _EchoLibby:
    """Stands in for Libby: echoes a modify, answers a show from ``values``."""

    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = values
        self.sent: List[Tuple[str, Dict[str, Any]]] = []

    def rpc(self, peer_id: str, key: str, payload: Dict[str, Any],
            ttl_ms: int = 8000) -> Dict[str, Any]:
        """Record the request; echo a modify, show a stored value."""
        # pylint: disable=unused-argument
        self.sent.append((key, payload))
        if key == "keys.describe":
            return {"status": "delivered",
                    "resp": {"ok": True, **self._values[payload["name"]].describe()}}
        if "value" in payload:
            return {"status": "delivered", "resp": {"ok": True, "value": payload["value"]}}
        return {"status": "delivered", "resp": {"ok": True, "value": self._values[key]}}

    def stop(self) -> None:
        """Nothing to release."""


def _run(argv: List[str], lib: _EchoLibby) -> Tuple[int, str, str]:
    namespace = libby_cli.build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(libby_cli, "load_cli_config", return_value={}), \
            mock.patch.object(libby_cli, "_mk_libby", return_value=lib), \
            mock.patch.object(libby_cli, "_modify_timeout", return_value=5.0), \
            redirect_stdout(out), redirect_stderr(err):
        rc = namespace.func(namespace)
    return rc, out.getvalue(), err.getvalue()


class ModifyDictTests(unittest.TestCase):
    """A JSON object on the command line travels as a dict."""

    def test_json_value_is_sent_as_a_dict(self):
        lib = _EchoLibby({})
        rc, out, _ = _run(["modify", "hsfei.stage.move", '{"x": 1.5, "fast": true}'], lib)
        self.assertEqual(rc, 0)
        self.assertEqual(lib.sent, [("move", {"value": {"x": 1.5, "fast": True}})])
        self.assertEqual(out.strip(), 'hsfei.stage.move = {"x": 1.5, "fast": true}')

    def test_equals_form_takes_a_json_value(self):
        lib = _EchoLibby({})
        rc, _, _ = _run(["modify", 'hsfei.stage.move={"x": 1}'], lib)
        self.assertEqual(rc, 0)
        self.assertEqual(lib.sent, [("move", {"value": {"x": 1}})])

    def test_malformed_json_fails_without_sending(self):
        lib = _EchoLibby({})
        rc, _, err = _run(["modify", "hsfei.stage.move", "{x: 1}"], lib)
        self.assertEqual(rc, 2)
        self.assertEqual(lib.sent, [])
        self.assertIn("not valid JSON", err)


class ShowDictTests(unittest.TestCase):
    """A dict value prints as JSON, so it can be pasted back into modify."""

    def test_text_output_is_json(self):
        rc, out, _ = _run(["show", "hsfei.stage.target"],
                          _EchoLibby({"target": {"x": 1.0, "label": "home"}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), 'hsfei.stage.target = {"x": 1.0, "label": "home"}')

    def test_json_output_keeps_the_dict_structured(self):
        _, out, _ = _run(["show", "hsfei.stage.target", "--json"],
                         _EchoLibby({"target": {"x": 1.0}}))
        self.assertEqual(json.loads(out)["value"], {"x": 1.0})


class DescribeDictTests(unittest.TestCase):
    """``libby describe`` lays a dict keyword's schema out as a table."""

    def _keyword(self) -> DictKeyword:
        return DictKeyword("move", setter=lambda v: None, description="Move the stage", fields={
            "x": Field("float", units="mm"),
            "y": Field("float", units="mm"),
            "speed": Field("float", units="mm/s", required=False, description="Slew rate"),
            "note": Field("string", required=False, nullable=True),
        })

    def test_text_output_lists_the_fields(self):
        rc, out, _ = _run(["describe", "hsfei.stage.move"],
                          _EchoLibby({"move": self._keyword()}))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[-5:], [
            "  fields:",
            "    x      float   required            mm",
            "    y      float   required            mm",
            "    speed  float   optional            mm/s  Slew rate",
            "    note   string  optional, nullable",
        ])

    def test_unused_columns_are_dropped(self):
        keyword = DictKeyword("pos", getter=dict, fields={"x": "float", "label": "string"})
        _, out, _ = _run(["describe", "hsfei.stage.pos"], _EchoLibby({"pos": keyword}))
        self.assertEqual(out.splitlines()[-3:], [
            "  fields:",
            "    x      float   required",
            "    label  string  required",
        ])

    def test_no_schema_prints_no_fields_section(self):
        keyword = DictKeyword("raw", setter=lambda v: None)
        _, out, _ = _run(["describe", "hsfei.stage.raw"], _EchoLibby({"raw": keyword}))
        self.assertNotIn("fields", out)

    def test_json_output_carries_the_schema(self):
        _, out, _ = _run(["describe", "hsfei.stage.move", "--json"],
                         _EchoLibby({"move": self._keyword()}))
        self.assertEqual(json.loads(out)["fields"]["speed"],
                         {"type": "float", "required": False, "nullable": False,
                          "units": "mm/s", "description": "Slew rate"})


if __name__ == "__main__":
    unittest.main()
