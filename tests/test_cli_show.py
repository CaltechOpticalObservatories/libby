"""Unit tests for ``libby show`` with a pattern. No transport needed.

The keys it asks a daemon for are visible through a recording stand-in for
``Libby``, which matters because a daemon drops a request for a key it does not
serve without replying, so a wrong key surfaces only as a timeout.
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List
from unittest import mock

from libby.cli import libby_cli


class _RecordingLibby:
    """Stands in for Libby, serving a fixed set of keywords and logging requests."""

    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = values
        self.requested: List[str] = []

    def rpc(self, peer_id: str, key: str, payload: Dict[str, Any],
            ttl_ms: int = 8000) -> Dict[str, Any]:
        """Answer keys.list and shows; time out on any other key."""
        # Arguments are ignored; the signature exists to match Libby
        # pylint: disable=unused-argument
        self.requested.append(key)
        if key == "keys.list":
            return {"status": "delivered",
                    "resp": {"ok": True, "matches": list(self._values)}}
        if key in self._values:
            return {"status": "delivered",
                    "resp": {"ok": True, "value": self._values[key]}}
        return {"status": "timeout"}

    def stop(self) -> None:
        """Nothing to release."""


class ShowPatternTests(unittest.TestCase):
    """Which keys a wildcard show asks for."""

    def _show(self, pattern: str, lib: _RecordingLibby) -> List[Dict[str, Any]]:
        namespace = libby_cli.build_parser().parse_args(["show", pattern, "--json"])
        out = io.StringIO()
        with mock.patch.object(libby_cli, "load_cli_config", return_value={}), \
                mock.patch.object(libby_cli, "_mk_libby", return_value=lib), \
                redirect_stdout(out):
            libby_cli.cmd_show(namespace)
        return json.loads(out.getvalue())

    def test_keeps_the_prefix_of_a_dotted_keyword(self):
        """Ask for adc.lastsample, not lastsample, which would time out."""
        lib = _RecordingLibby({"adc.lastsample": "2026-11-02T05:18:42+00:00",
                               "yj1.lastsample": "2026-11-02T05:18:40+00:00"})
        rows = self._show("hispec.keygrabber.%.lastsample", lib)

        self.assertEqual(lib.requested[1:], ["adc.lastsample", "yj1.lastsample"])
        self.assertTrue(all(row["ok"] for row in rows))
        self.assertEqual(rows[0]["qualified"], "hispec.keygrabber.adc.lastsample")

    def test_reads_a_plain_keyword(self):
        lib = _RecordingLibby({"readerrors": 65})
        rows = self._show("hispec.keygrabber.%", lib)

        self.assertEqual(lib.requested[1:], ["readerrors"])
        self.assertEqual(rows[0]["value"], 65)


if __name__ == "__main__":
    unittest.main()
