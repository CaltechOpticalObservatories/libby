"""Unit tests for unwrap()'s envelope -> value-or-raise reduction. No transport needed."""
import unittest

from libby.errors import KeywordError, LibbyError, LibbyTimeout
from libby.response import unwrap


class UnwrapTests(unittest.TestCase):
    def test_delivered_ok_returns_the_keyword_response(self):
        envelope = {"status": "delivered", "resp": {"ok": True, "value": 79.0, "units": "mm"}}
        self.assertEqual(unwrap("hsfei.pickoff.positionvalue", envelope),
                          {"ok": True, "value": 79.0, "units": "mm"})

    def test_timeout_status_raises_libby_timeout(self):
        envelope = {"status": "timeout"}
        with self.assertRaises(LibbyTimeout):
            unwrap("hsfei.pickoff.positionvalue", envelope)

    def test_delivered_with_no_response_raises_libby_timeout(self):
        envelope = {"status": "delivered", "resp": None}
        with self.assertRaises(LibbyTimeout):
            unwrap("hsfei.pickoff.positionvalue", envelope)

    def test_too_large_raises_libby_error(self):
        envelope = {"status": "too_large", "mtu": 1024, "size": 2048}
        with self.assertRaises(LibbyError):
            unwrap("hsfei.pickoff.positionvalue", envelope)

    def test_daemon_rejection_raises_keyword_error_with_name_and_message(self):
        envelope = {"status": "delivered", "resp": {"ok": False, "error": "keyword is read-only"}}
        with self.assertRaises(KeywordError) as ctx:
            unwrap("hsfei.pickoff.isreferenced", envelope)
        self.assertEqual(ctx.exception.name, "hsfei.pickoff.isreferenced")
        self.assertEqual(ctx.exception.error, "keyword is read-only")

    def test_non_dict_envelope_raises_libby_error(self):
        with self.assertRaises(LibbyError):
            unwrap("hsfei.pickoff.positionvalue", None)

    def test_non_dict_resp_raises_libby_error(self):
        envelope = {"status": "delivered", "resp": "not a dict"}
        with self.assertRaises(LibbyError):
            unwrap("hsfei.pickoff.positionvalue", envelope)


if __name__ == "__main__":
    unittest.main()
