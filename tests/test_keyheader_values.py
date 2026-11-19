"""Unit tests for keyheader value acquisition and card entry building."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, List

from libby.keygrabber import Sample
from libby.keyheader import (Acquired, Mode, ValueType, acquire, card_entry,
                             fits_date, query_range, segment_entries)
from libby.keyheader.config import CardConfig, SegmentConfig

T0 = datetime(2026, 11, 11, 8, 15, 0, tzinfo=timezone.utc)


def _at(seconds: float, value: Any) -> Sample:
    return Sample(keyword="positionvalue", group="hsfei", peer="adc",
                  value=value, units="mm",
                  timestamp=T0 + timedelta(seconds=seconds))


def _card(kind: ValueType = ValueType.FLOAT, fmt=None,
          comment: str = "ADC position") -> CardConfig:
    return CardConfig(fits="ADCPOS", group="hsfei", peer="adc",
                      keyword="positionvalue", type=kind, format=fmt,
                      comment=comment, mode=Mode.NEAREST, window_s=60.0)


SAMPLES: List[Sample] = [_at(-10, 1.0), _at(-4, 2.0), _at(6, 4.0), _at(20, 9.0)]


class QueryRangeTests(unittest.TestCase):
    """Each mode fetches only the side of the moment it can use."""

    def test_prior_includes_the_moment(self):
        """Run just past the moment, since a source's stop is exclusive."""
        start, stop = query_range(T0, Mode.PRIOR, 60)
        self.assertEqual(start, T0 - timedelta(seconds=60))
        self.assertGreater(stop, T0)
        self.assertLess(stop - T0, timedelta(milliseconds=1))

    def test_subsequent_starts_at_the_moment(self):
        """Start exactly at the moment."""
        self.assertEqual(query_range(T0, Mode.SUBSEQUENT, 60),
                         (T0, T0 + timedelta(seconds=60)))

    def test_nearest_and_interpolate_span_both_sides(self):
        """Look a window either side."""
        for mode in (Mode.NEAREST, Mode.INTERPOLATE):
            self.assertEqual(query_range(T0, mode, 60),
                             (T0 - timedelta(seconds=60),
                              T0 + timedelta(seconds=60)))


class AcquireTests(unittest.TestCase):
    """Each mode picks the right sample, or says why it cannot."""

    def test_prior(self):
        """Take the latest sample at or before the moment."""
        self.assertEqual(acquire(SAMPLES, T0, Mode.PRIOR), Acquired(2.0))

    def test_subsequent(self):
        """Take the earliest sample at or after the moment."""
        self.assertEqual(acquire(SAMPLES, T0, Mode.SUBSEQUENT), Acquired(4.0))

    def test_nearest(self):
        """Take whichever side is closer in time."""
        self.assertEqual(acquire(SAMPLES, T0, Mode.NEAREST), Acquired(2.0))
        self.assertEqual(acquire(SAMPLES, T0 + timedelta(seconds=2),
                                 Mode.NEAREST), Acquired(4.0))

    def test_nearest_tie_prefers_earlier(self):
        """Break a tie toward what was already true at the moment."""
        self.assertEqual(acquire(SAMPLES, T0 + timedelta(seconds=1),
                                 Mode.NEAREST), Acquired(2.0))

    def test_interpolate(self):
        """Interpolate linearly between the neighbours."""
        result = acquire(SAMPLES, T0, Mode.INTERPOLATE)
        self.assertAlmostEqual(result.value, 2.0 + 2.0 * 0.4)

    def test_exact_hit(self):
        """Return a sample stamped exactly at the moment, in every mode."""
        samples = [_at(-5, 1.0), _at(0, 3.0), _at(5, 5.0)]
        for mode in Mode:
            with self.subTest(mode=mode):
                self.assertEqual(acquire(samples, T0, mode), Acquired(3.0))

    def test_interpolate_falls_back_to_one_side(self):
        """Use the last value when no later sample is stored yet."""
        before_only = [s for s in SAMPLES if s.timestamp <= T0]
        self.assertEqual(acquire(before_only, T0, Mode.INTERPOLATE),
                         Acquired(2.0))

    def test_interpolate_non_numeric(self):
        """Refuse to interpolate strings, rather than raising."""
        samples = [_at(-1, "a"), _at(1, "b")]
        self.assertEqual(acquire(samples, T0, Mode.INTERPOLATE).reason,
                         "not numeric")

    def test_missing_reasons(self):
        """Say which side was missing."""
        self.assertEqual(acquire([], T0, Mode.NEAREST).reason, "no sample")
        self.assertEqual(acquire([_at(5, 1.0)], T0, Mode.PRIOR).reason,
                         "no prior sample")
        self.assertEqual(acquire([_at(-5, 1.0)], T0, Mode.SUBSEQUENT).reason,
                         "no later sample")

    def test_nulls_are_ignored(self):
        """Skip a null, which says nothing about the keyword's state."""
        samples = [_at(-5, 1.0), _at(-1, None)]
        self.assertEqual(acquire(samples, T0, Mode.PRIOR), Acquired(1.0))


class CardEntryTests(unittest.TestCase):
    """Values become typed entries; problems become blank cards."""

    def test_float_with_format(self):
        """Apply the format's precision and keep a number."""
        entry = card_entry(_card(fmt=".3f"), Acquired(1.23456))
        self.assertEqual(entry, {"keyword": "ADCPOS", "value": 1.235,
                                 "comment": "ADC position"})

    def test_int_rounds_stored_float(self):
        """Round, because InfluxDB stores every integer as a float."""
        entry = card_entry(_card(ValueType.INT), Acquired(41.9999))
        self.assertEqual(entry["value"], 42)
        self.assertIsInstance(entry["value"], int)

    def test_bool(self):
        """Accept a bool, or a 0/1 a backend stored as a number."""
        self.assertIs(card_entry(_card(ValueType.BOOL), Acquired(True))["value"],
                      True)
        self.assertIs(card_entry(_card(ValueType.BOOL), Acquired(0.0))["value"],
                      False)

    def test_str_with_format(self):
        """Apply a string format, e.g. to truncate."""
        entry = card_entry(_card(ValueType.STR, fmt=".5"), Acquired("Ready now"))
        self.assertEqual(entry["value"], "Ready")

    def test_missing_value_is_blank_with_reason(self):
        """Keep the card, with no value and a comment saying why."""
        entry = card_entry(_card(), Acquired.missing("no sample"))
        self.assertEqual(entry, {"keyword": "ADCPOS", "value": None,
                                 "comment": "no sample: ADC position"})

    def test_wrong_type_is_blank(self):
        """Blank a value that cannot be the configured type."""
        self.assertEqual(card_entry(_card(), Acquired("text"))["comment"],
                         "not a float: ADC position")
        self.assertEqual(
            card_entry(_card(ValueType.BOOL), Acquired(2))["comment"],
            "not a bool: ADC position")

    def test_non_finite_is_blank(self):
        """Blank NaN and infinity, which FITS cannot represent."""
        entry = card_entry(_card(), Acquired(float("nan")))
        self.assertIsNone(entry["value"])
        self.assertTrue(entry["comment"].startswith("not finite"))

    def test_blank_comment_is_truncated_to_fit(self):
        """Keep the combined reason and comment within one card."""
        entry = card_entry(_card(comment="x" * 47), Acquired.missing("no sample"))
        self.assertEqual(len(entry["comment"]), 47)


class SegmentEntryTests(unittest.TestCase):
    """A segment starts with its COMMENT and date keyword."""

    def test_layout(self):
        """COMMENT, then the date keyword, then cards in config order."""
        segment = SegmentConfig(name="EXP-BEGIN", date_keyword="DATE-BEG",
                                date_comment="start", cards=(_card(),))
        moment = T0 + timedelta(microseconds=123456)
        entries = segment_entries(segment, moment, [Acquired(1.5)])
        self.assertEqual(entries[0], {"keyword": "COMMENT",
                                      "value": "EXP-BEGIN 2026-11-11T08:15:00.123",
                                      "comment": ""})
        self.assertEqual(entries[1], {"keyword": "DATE-BEG",
                                      "value": "2026-11-11T08:15:00.123",
                                      "comment": "start"})
        self.assertEqual(entries[2]["keyword"], "ADCPOS")

    def test_fits_date_converts_to_utc(self):
        """Write the date in UTC whatever zone the moment arrived in."""
        pacific = timezone(timedelta(hours=-8))
        self.assertEqual(fits_date(datetime(2026, 11, 11, 0, 0, tzinfo=pacific)),
                         "2026-11-11T08:00:00.000")


if __name__ == "__main__":
    unittest.main()
