"""Unit tests for keyheader config parsing and FITS keyword validation."""
from __future__ import annotations

import copy
import os
import unittest
from typing import Any, Dict
from unittest import mock

import yaml

from libby.config import ConfigError
from libby.keyheader import Mode, ValueType, is_reserved, parse_config
from libby.keyheader.config import DEFAULT_WINDOW_S, MAX_COMMENT, build_source

BASE: Dict[str, Any] = {
    "peer_id": "keyheader",
    "group_id": "hispec",
    "defaults": {"mode": "nearest", "window_s": 120},
    "segments": [
        {
            "name": "EXP-BEGIN",
            "date_keyword": "DATE-BEG",
            "cards": [
                {"fits": "ADCPOS1", "keyword": "hsfei.adc.positionvalue1",
                 "type": "float", "format": ".3f", "comment": "ADC position",
                 "mode": "interpolate"},
                {"fits": "ADCMOVE", "keyword": "hsfei.adc.ismoving",
                 "type": "bool"},
            ],
        },
        {
            "name": "EXP-END",
            "date_keyword": "DATE-END",
            "cards": [
                {"fits": "ADCPOS1E", "keyword": "hsfei.adc.positionvalue1",
                 "type": "float", "mode": "prior", "window_s": 30},
            ],
        },
    ],
}


def _config(**changes: Any) -> Dict[str, Any]:
    config = copy.deepcopy(BASE)
    config.update(changes)
    return config


def _with_card(**fields: Any) -> Dict[str, Any]:
    """Return the base config with one more card in EXP-BEGIN."""
    config = _config()
    card = {"fits": "EXTRA", "keyword": "hsfei.adc.x", "type": "float"}
    card.update(fields)
    config["segments"][0]["cards"].append(card)
    return config


class ParseTests(unittest.TestCase):
    """A valid config parses into typed segments and cards."""

    def test_segments_and_cards_keep_config_order(self):
        """Order matters: it is the order the cards appear in the header."""
        settings = parse_config(_config())
        self.assertEqual([s.name for s in settings.segments],
                         ["EXP-BEGIN", "EXP-END"])
        self.assertEqual([c.fits for c in settings.segments[0].cards],
                         ["ADCPOS1", "ADCMOVE"])

    def test_card_fields_are_typed(self):
        """Split the source keyword and convert type and mode to enums."""
        card = parse_config(_config()).segments[0].cards[0]
        self.assertEqual((card.group, card.peer, card.keyword),
                         ("hsfei", "adc", "positionvalue1"))
        self.assertIs(card.type, ValueType.FLOAT)
        self.assertIs(card.mode, Mode.INTERPOLATE)
        self.assertEqual(card.format, ".3f")

    def test_defaults_and_overrides(self):
        """Fall back to defaults, and let a card override them."""
        settings = parse_config(_config())
        moving = settings.segments[0].cards[1]
        self.assertIs(moving.mode, Mode.NEAREST)
        self.assertEqual(moving.window_s, 120.0)
        self.assertEqual(settings.segments[1].cards[0].window_s, 30.0)

    def test_builtin_default_window(self):
        """Use the built-in window when neither defaults nor the card set one."""
        config = _config(defaults={})
        card = parse_config(config).segments[0].cards[1]
        self.assertEqual(card.window_s, DEFAULT_WINDOW_S)

    def test_segment_defaults_override_global(self):
        """Let a segment set defaults for its own cards."""
        config = _config()
        config["segments"][1]["defaults"] = {"mode": "prior"}
        del config["segments"][1]["cards"][0]["mode"]
        self.assertIs(parse_config(config).segments[1].cards[0].mode, Mode.PRIOR)

    def test_lookup_by_name(self):
        """Find a segment by name, and name the known ones when missing."""
        settings = parse_config(_config())
        self.assertEqual(settings.segment("EXP-END").date_keyword, "DATE-END")
        with self.assertRaisesRegex(ValueError, "EXP-BEGIN, EXP-END"):
            settings.segment("EXP-MID")

    def test_date_keyword_is_optional(self):
        """Leave the date keyword out when a segment does not name one."""
        config = _config()
        del config["segments"][0]["date_keyword"]
        self.assertIsNone(parse_config(config).segments[0].date_keyword)


class UniquenessTests(unittest.TestCase):
    """Duplicates are errors, never silently renamed or merged."""

    def test_duplicate_within_a_segment(self):
        """Reject the same FITS keyword twice in one segment."""
        with self.assertRaisesRegex(ConfigError,
                                    "ADCPOS1 appears twice in segment"):
            parse_config(_with_card(fits="ADCPOS1"))

    def test_duplicate_across_segments(self):
        """Reject a keyword two segments share, since they merge into one header."""
        config = _config()
        config["segments"][1]["cards"][0]["fits"] = "ADCPOS1"
        with self.assertRaisesRegex(ConfigError,
                                    "'EXP-BEGIN' and 'EXP-END'"):
            parse_config(config)

    def test_date_keyword_counts_toward_uniqueness(self):
        """Reject two segments that both claim DATE-BEG."""
        config = _config()
        config["segments"][1]["date_keyword"] = "DATE-BEG"
        with self.assertRaisesRegex(ConfigError, "DATE-BEG appears"):
            parse_config(config)

    def test_duplicate_segment_name(self):
        """Reject two segments with the same name."""
        config = _config()
        config["segments"][1]["name"] = "EXP-BEGIN"
        with self.assertRaisesRegex(ConfigError, "appears twice"):
            parse_config(config)

    def test_mapping_form_is_refused(self):
        """Refuse mappings, where YAML would hide a duplicate key."""
        text = """
segments:
  - name: S
    cards:
      ADCPOS1: {keyword: a.b.c, type: float}
      ADCPOS1: {keyword: a.b.d, type: float}
"""
        with self.assertRaisesRegex(ConfigError, "not a mapping"):
            parse_config(yaml.safe_load(text))
        with self.assertRaisesRegex(ConfigError, "not a mapping"):
            parse_config({"segments": {"S": {"cards": []}}})


class KeywordValidationTests(unittest.TestCase):
    """FITS keyword names follow the standard."""

    def test_rejects_malformed_names(self):
        """Reject lowercase, over-long and illegal-character names."""
        for name in ("adcpos", "TOOLONGNAME", "ADC.POS", "ADC POS"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigError, "1-8 characters"):
                    parse_config(_with_card(fits=name))

    def test_accepts_hyphen_and_underscore(self):
        """Allow the full FITS character set."""
        parse_config(_with_card(fits="AD-C_9"))

    def test_rejects_reserved(self):
        """Reject structural, commentary, WCS and time keywords."""
        for name in ("SIMPLE", "NAXIS2", "COMMENT", "HISTORY", "CONTINUE",
                     "END", "CTYPE1", "CRVAL2A", "PC1_2", "DATE-OBS",
                     "DATE-BEG", "OBJECT", "BZERO", "TFORM12", "OBSGEO-X"):
            with self.subTest(name=name):
                self.assertTrue(is_reserved(name))
                with self.assertRaisesRegex(ConfigError, "reserved"):
                    parse_config(_with_card(fits=name))

    def test_near_misses_are_not_reserved(self):
        """Do not over-match: these merely resemble reserved names."""
        for name in ("NAXISX", "EXPTIME", "CTYPE", "DATEX", "ADCPOS1"):
            with self.subTest(name=name):
                self.assertFalse(is_reserved(name))

    def test_date_keyword_must_be_a_time_keyword(self):
        """Let a segment name only one of the standard date keywords."""
        config = _config()
        config["segments"][0]["date_keyword"] = "STARTTM"
        with self.assertRaisesRegex(ConfigError, "date_keyword"):
            parse_config(config)


class CardValidationTests(unittest.TestCase):
    """Per-card field checks."""

    def test_source_keyword_must_be_qualified(self):
        """Require group, daemon and keyword."""
        for keyword in ("adc.positionvalue", "positionvalue", "hsfei.adc.%"):
            with self.subTest(keyword=keyword):
                with self.assertRaisesRegex(ConfigError, "<group>.<daemon>"):
                    parse_config(_with_card(keyword=keyword))

    def test_unknown_type_and_mode(self):
        """Name the accepted choices."""
        with self.assertRaisesRegex(ConfigError, "float, int, bool, str"):
            parse_config(_with_card(type="double"))
        with self.assertRaisesRegex(ConfigError, "interpolate, nearest"):
            parse_config(_with_card(mode="closest"))

    def test_no_interpolating_bool_or_str(self):
        """Refuse to interpolate a value that has no in-between."""
        for kind in ("bool", "str"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ConfigError, "cannot interpolate"):
                    parse_config(_with_card(type=kind, mode="interpolate"))

    def test_format_is_checked_against_type(self):
        """Reject a spec the type cannot take, or one that will not parse back."""
        with self.assertRaisesRegex(ConfigError, "not valid"):
            parse_config(_with_card(type="int", format=".3s"))
        with self.assertRaisesRegex(ConfigError, "read back"):
            parse_config(_with_card(type="float", format=",.2f"))
        with self.assertRaisesRegex(ConfigError, "bool"):
            parse_config(_with_card(type="bool", format="d"))

    def test_comment_length(self):
        """Reject a comment that would be truncated on the card."""
        parse_config(_with_card(comment="x" * MAX_COMMENT))
        with self.assertRaisesRegex(ConfigError, "at most"):
            parse_config(_with_card(comment="x" * (MAX_COMMENT + 1)))

    def test_non_positive_window(self):
        """Reject a window that could never contain a sample."""
        with self.assertRaisesRegex(ConfigError, "positive"):
            parse_config(_with_card(window_s=0))

    def test_empty_segments(self):
        """Require at least one segment, and at least one card in each."""
        with self.assertRaisesRegex(ConfigError, "non-empty"):
            parse_config(_config(segments=[]))
        with self.assertRaisesRegex(ConfigError, "non-empty"):
            parse_config(_config(segments=[{"name": "S", "cards": []}]))


class BuildSourceTests(unittest.TestCase):
    """The source section is validated like the keygrabber's sink."""

    SOURCE = {"type": "influxdb", "url": "http://influx:8086", "org": "o",
              "bucket": "b", "token_env": "KEYHEADER_TEST_TOKEN"}

    def test_token_in_file_is_rejected(self):
        """Keep the credential out of the config file."""
        with self.assertRaisesRegex(ConfigError, "source.token must not appear"):
            build_source({**self.SOURCE, "token": "secret"})

    def test_missing_token_env(self):
        """Fail clearly when the named variable is unset."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "source has no token"):
                build_source(self.SOURCE)

    def test_unsupported_type(self):
        """Name the one backend that exists."""
        with self.assertRaisesRegex(ConfigError, "unsupported source type"):
            build_source({**self.SOURCE, "type": "sqlite"})


if __name__ == "__main__":
    unittest.main()
