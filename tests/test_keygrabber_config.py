"""Unit tests for keygrabber config parsing, validation and keyword selection."""
from __future__ import annotations

import os
import unittest
from typing import Any, Dict
from unittest import mock

from libby.config import ConfigError
from libby.keygrabber import parse_config, select_keywords
from libby.keygrabber.config import DEFAULT_EXCLUDE, build_sink

PEER_KEYWORDS = [
    "isconnected", "ismoving", "isreferenced", "lasterror",
    "positionvalue1", "positionvalue2", "softmax", "uptime",
]


def _config(**overrides: Any) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "peer_id": "keygrabber",
        "group_id": "hispec",
        "defaults": {"interval_s": 10.0, "timeout_s": 2.0, "refresh_s": 300.0},
        "collections": {
            "adc": {"peer": "hsfei.adc", "keywords": ["positionvalue%"]},
        },
    }
    config.update(overrides)
    return config


class ParseConfigTests(unittest.TestCase):
    """Validation of the collections and defaults sections."""

    def test_defaults_are_inherited(self):
        """Fall back to the defaults section for unset cadences."""
        collection = parse_config(_config()).collections[0]
        self.assertEqual(collection.interval_s, 10.0)
        self.assertEqual(collection.timeout_s, 2.0)
        self.assertEqual(collection.refresh_s, 300.0)

    def test_collection_overrides_defaults(self):
        """Let one collection set its own cadence."""
        config = _config(collections={
            "adc": {"peer": "hsfei.adc", "keywords": ["%"], "interval_s": 60.0},
        })
        self.assertEqual(parse_config(config).collections[0].interval_s, 60.0)

    def test_peer_is_split_into_group_and_daemon(self):
        """Keep group and daemon apart, since they become separate tags."""
        collection = parse_config(_config()).collections[0]
        self.assertEqual((collection.group, collection.daemon), ("hsfei", "adc"))
        self.assertEqual(collection.peer, "hsfei.adc")

    def test_collections_are_ordered_by_name(self):
        """Parse deterministically, so two runs agree."""
        config = _config(collections={
            "zzz": {"peer": "hsfei.z", "keywords": ["%"]},
            "aaa": {"peer": "hsfei.a", "keywords": ["%"]},
        })
        names = [c.name for c in parse_config(config).collections]
        self.assertEqual(names, ["aaa", "zzz"])

    def test_interval_must_clear_the_timeout(self):
        """Reject a cadence one unanswered read would overrun.

        bamboo waits timeout_s for the ACK and timeout_s/2 more for the reply,
        so 2s of timeout can occupy a worker for 3s.
        """
        config = _config(collections={
            "adc": {"peer": "hsfei.adc", "keywords": ["%"],
                    "interval_s": 3.0, "timeout_s": 2.0},
        })
        with self.assertRaises(ConfigError) as caught:
            parse_config(config)
        self.assertIn("interval_s", str(caught.exception))

    def test_interval_just_above_the_headroom_is_accepted(self):
        """Allow a cadence that clears 1.5x the timeout."""
        config = _config(collections={
            "adc": {"peer": "hsfei.adc", "keywords": ["%"],
                    "interval_s": 3.1, "timeout_s": 2.0},
        })
        self.assertEqual(parse_config(config).collections[0].interval_s, 3.1)

    def test_collection_name_must_compose_into_a_keyword(self):
        """Reject a name that could not become a control keyword prefix."""
        config = _config(collections={
            "ADC Rotator": {"peer": "hsfei.adc", "keywords": ["%"]},
        })
        with self.assertRaises(ConfigError):
            parse_config(config)

    def test_peer_must_be_group_and_daemon(self):
        """Reject a peer that is not exactly '<group>.<daemon>'."""
        for peer in ("adc", "hsfei.adc.extra", "hsfei.", "hsfei.%"):
            with self.subTest(peer=peer):
                config = _config(collections={
                    "adc": {"peer": peer, "keywords": ["%"]},
                })
                with self.assertRaises(ConfigError):
                    parse_config(config)

    def test_keywords_are_required(self):
        """Refuse a collection that selects nothing."""
        for keywords in (None, [], "positionvalue", [""], [1]):
            with self.subTest(keywords=keywords):
                entry: Dict[str, Any] = {"peer": "hsfei.adc"}
                if keywords is not None:
                    entry["keywords"] = keywords
                with self.assertRaises(ConfigError):
                    parse_config(_config(collections={"adc": entry}))

    def test_non_numeric_cadence_is_rejected(self):
        """Reject a cadence that is not a number."""
        config = _config(collections={
            "adc": {"peer": "hsfei.adc", "keywords": ["%"], "interval_s": "soon"},
        })
        with self.assertRaises(ConfigError):
            parse_config(config)

    def test_workers_must_be_a_positive_integer(self):
        """Reject a pool that could never run a tick."""
        for workers in (0, -1, 1.5, True, "four"):
            with self.subTest(workers=workers):
                with self.assertRaises(ConfigError):
                    parse_config(_config(workers=workers))

    def test_retry_bounds_are_reported_as_config_errors(self):
        """Surface a bad retry policy as a config error, not a ValueError."""
        with self.assertRaises(ConfigError):
            parse_config(_config(retry={"max_batches": 0}))

    def test_missing_collections_section_is_allowed(self):
        """Let a daemon start with nothing configured yet."""
        config = _config()
        del config["collections"]
        self.assertEqual(parse_config(config).collections, ())


class SelectKeywordsTests(unittest.TestCase):
    """Include and exclude patterns applied to a peer's keyword list."""

    def _selected(self, keywords, exclude=None):
        collection = parse_config(_config(collections={
            "adc": {"peer": "hsfei.adc", "keywords": keywords,
                    **({"exclude": exclude} if exclude else {})},
        })).collections[0]
        return select_keywords(collection, PEER_KEYWORDS)

    def test_wildcard_selects_matching_names(self):
        """Match a trailing wildcard within a single name."""
        self.assertEqual(self._selected(["positionvalue%"]),
                         ("positionvalue1", "positionvalue2"))

    def test_several_patterns_are_unioned_without_duplicates(self):
        """Union patterns, keeping each name once."""
        selected = self._selected(["is%", "isconnected"])
        self.assertEqual(selected, ("isconnected", "ismoving", "isreferenced"))

    def test_explicit_exclude_is_applied_after_includes(self):
        """Drop an excluded name a wildcard would otherwise have matched."""
        self.assertEqual(self._selected(["is%"], exclude=["ismoving"]),
                         ("isconnected", "isreferenced"))

    def test_noisy_keywords_are_excluded_by_default(self):
        """Keep uptime and lasterror out of a select-everything collection.

        uptime changes every second and says nothing a timestamp does not, and
        lasterror is null most of the time.
        """
        selected = self._selected(["%"])
        for name in DEFAULT_EXCLUDE:
            self.assertNotIn(name, selected)
        self.assertIn("positionvalue1", selected)

    def test_naming_a_default_exclusion_opts_back_in(self):
        """Honour an explicit request for a keyword the default would drop."""
        self.assertEqual(self._selected(["uptime"]), ("uptime",))

    def test_no_matches_selects_nothing(self):
        """Return empty rather than failing when a pattern matches nothing."""
        self.assertEqual(self._selected(["nosuch%"]), ())


class BuildSinkTests(unittest.TestCase):
    """Sink construction from the config's sink section."""

    def test_token_in_the_config_is_refused(self):
        """Refuse an inline credential so it cannot reach a config file."""
        with self.assertRaises(ConfigError) as caught:
            build_sink({"type": "influxdb", "url": "http://x", "org": "o",
                        "bucket": "b", "token": "secret"})
        self.assertIn("token_env", str(caught.exception))

    def test_unset_token_env_is_reported(self):
        """Name the missing variable rather than failing at write time."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as caught:
                build_sink({"type": "influxdb", "url": "http://x", "org": "o",
                            "bucket": "b", "token_env": "NOT_SET_ANYWHERE"})
        self.assertIn("NOT_SET_ANYWHERE", str(caught.exception))

    def test_missing_type_is_reported(self):
        """Require an explicit sink type."""
        with self.assertRaises(ConfigError):
            build_sink({})

    def test_unknown_type_is_reported(self):
        """Name the unsupported backend."""
        with self.assertRaises(ConfigError) as caught:
            build_sink({"type": "sqlite"})
        self.assertIn("sqlite", str(caught.exception))

    def test_missing_connection_fields_are_reported(self):
        """Require url, org and bucket before reaching the client."""
        with mock.patch.dict(os.environ, {"TOKEN": "t"}, clear=True):
            with self.assertRaises(ConfigError):
                build_sink({"type": "influxdb", "token_env": "TOKEN"})


if __name__ == "__main__":
    unittest.main()
