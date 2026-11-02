"""Unit tests for where a daemon's log records go. No transport needed.

A daemon without a ``logging`` section must still be heard: Python prints
warnings and errors on stderr only while a logger has no handlers, so the
``lasterror`` capture must not be one. The keygrabber's entry point adds INFO
on top, unless the config's own section already decides.
"""
from __future__ import annotations

import logging
import unittest
from typing import Any, Dict, Optional
from unittest import mock

from libby.daemon import LibbyDaemon
from libby.keygrabber import main as keygrabber_main

# lasterror is read back from the daemon's own attribute
# pylint: disable=protected-access


class LastErrorCaptureTests(unittest.TestCase):
    """The lasterror keyword's source, and what it leaves alone."""

    def setUp(self) -> None:
        # Loggers are global by name, so each test gets one of its own
        self.daemon = LibbyDaemon.from_config({"peer_id": f"logtest-{self.id()}"})

    def tearDown(self) -> None:
        self.daemon.logger.filters.clear()

    def test_adds_no_handler_without_a_logging_section(self):
        """Leave Python's default stderr output for warnings and errors working."""
        self.assertEqual(self.daemon.logger.handlers, [])

    def test_captures_the_latest_error(self):
        self.daemon.logger.error("first")
        self.daemon.logger.error("collection %s failed", "adc")
        self.assertEqual(self.daemon._last_error, "collection adc failed")

    def test_ignores_records_below_error(self):
        self.daemon.logger.warning("only a warning")
        self.assertIsNone(self.daemon._last_error)

    def test_keeps_the_traceback_of_a_logged_exception(self):
        try:
            raise RuntimeError("hardware unreachable")
        except RuntimeError:
            self.daemon.logger.exception("daemon initialization failed")
        self.assertIn("daemon initialization failed", self.daemon._last_error)
        self.assertIn("RuntimeError: hardware unreachable", self.daemon._last_error)

    def test_attaches_once(self):
        self.daemon._setup_logging()
        self.assertEqual(len(self.daemon.logger.filters), 1)


class _StubDaemon:
    """Stands in for KeygrabberDaemon, holding a config and returning at once."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config

    def get_config(self, key: str, default: Any = None) -> Any:
        """Return a top-level config value."""
        return self._config.get(key, default)

    def serve(self) -> None:
        """Return immediately, as if stopped."""


class KeygrabberEntryPointTests(unittest.TestCase):
    """Whether ``keygrabber`` sends INFO to stderr itself."""

    def _run(self, logging_section: Optional[Dict[str, Any]]) -> mock.Mock:
        config = {} if logging_section is None else {"logging": logging_section}
        with mock.patch.object(keygrabber_main.KeygrabberDaemon, "from_config_file",
                               return_value=_StubDaemon(config)), \
                mock.patch.object(keygrabber_main, "log_to_stderr") as log_to_stderr:
            self.assertEqual(keygrabber_main.main(["-c", "keygrabber.yaml"]), 0)
        return log_to_stderr

    def test_logs_to_stderr_without_a_logging_section(self):
        self._run(None).assert_called_once_with()

    def test_leaves_a_configured_logging_section_alone(self):
        """Do not add a root handler that would print every line twice."""
        self._run({"level": "DEBUG"}).assert_not_called()

    def test_quiets_pika_connection_chatter(self):
        pika = logging.getLogger("pika")
        level = pika.level
        with mock.patch.object(keygrabber_main.logging, "basicConfig"):
            keygrabber_main.log_to_stderr()
        try:
            self.assertEqual(pika.level, logging.WARNING)
        finally:
            pika.setLevel(level)


if __name__ == "__main__":
    unittest.main()
