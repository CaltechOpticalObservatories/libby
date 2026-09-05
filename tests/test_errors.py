"""Unit tests for the exception hierarchy. No transport needed."""
import unittest

from libby.errors import ConfigError, KeywordError, KeywordNameError, LibbyError, LibbyTimeout
from libby.config import ConfigError as DaemonConfigError


class ExceptionHierarchyTests(unittest.TestCase):
    def test_all_public_errors_are_libby_errors(self):
        for exc_type in (ConfigError, KeywordNameError, LibbyTimeout, KeywordError):
            self.assertTrue(issubclass(exc_type, LibbyError))

    def test_keyword_error_carries_name_and_message(self):
        exc = KeywordError("hsfei.pickoff.isreferenced", "keyword is read-only")
        self.assertEqual(exc.name, "hsfei.pickoff.isreferenced")
        self.assertEqual(exc.error, "keyword is read-only")

    def test_daemon_config_error_is_also_the_public_config_error(self):
        # libby.config.ConfigError (daemon subsystem-config loading) and
        # libby.errors.ConfigError (client cli_config.yaml loading) used to
        # be two unrelated exceptions with the same name; catching one should
        # catch both regardless of which loader raised it.
        self.assertTrue(issubclass(DaemonConfigError, ConfigError))
        self.assertTrue(issubclass(DaemonConfigError, ValueError))
        with self.assertRaises(ConfigError):
            raise DaemonConfigError("bad daemon config")


if __name__ == "__main__":
    unittest.main()
