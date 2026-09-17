"""Parse and validate the keygrabber's own configuration sections.

A keygrabber config file is an ordinary ``LibbyDaemon`` config (``peer_id``,
``group_id``, ``transport``, ...) plus three sections this module owns:
``sink``, ``defaults`` and ``collections``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence, Tuple

from ..config import ConfigError
from ..keyword import match_pattern
from .sink import DEFAULT_MAX_BATCHES, RetryPolicy, Sink

# Keywords no collection should record unless it names one explicitly.
# ``uptime`` changes every second and carries no information a timestamp does
# not already give; ``lasterror`` is null most of the time and Influx cannot
# store a null field.
DEFAULT_EXCLUDE: Tuple[str, ...] = ("lasterror", "uptime")

DEFAULT_INTERVAL_S = 10.0
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_REFRESH_S = 300.0
DEFAULT_WORKERS = 4

# bamboo waits ``timeout_s`` for the ACK and a further ``timeout_s / 2`` for
# the response, so one wedged peer can occupy a worker for 1.5x the timeout.
TIMEOUT_HEADROOM = 1.5

_COLLECTION_NAME = re.compile(r"^[a-z0-9_]+$")


# A config record, so the field count is the schema rather than complexity
@dataclass(frozen=True)
class CollectionConfig:  # pylint: disable=too-many-instance-attributes
    """One configured peer, keyword selection and cadence."""

    name: str
    group: str
    daemon: str
    keywords: Tuple[str, ...]
    exclude: Tuple[str, ...]
    interval_s: float
    timeout_s: float
    refresh_s: float

    @property
    def peer(self) -> str:
        """Return the qualified peer address this collection reads."""
        return f"{self.group}.{self.daemon}"

    def excluded(self) -> Tuple[str, ...]:
        """Return every exclusion, including the defaults not named explicitly."""
        defaults = tuple(name for name in DEFAULT_EXCLUDE
                         if name not in self.keywords)
        return self.exclude + defaults


@dataclass(frozen=True)
class KeygrabberConfig:
    """The keygrabber's parsed configuration."""

    collections: Tuple[CollectionConfig, ...]
    sink: Mapping[str, Any]
    workers: int
    retry: RetryPolicy


def parse_config(config: Mapping[str, Any]) -> KeygrabberConfig:
    """Validate a daemon config's keygrabber sections into typed objects."""
    defaults = _mapping(config, "defaults")
    collections = _mapping(config, "collections")

    parsed = tuple(
        _parse_collection(name, collections[name], defaults)
        for name in sorted(collections)
    )
    return KeygrabberConfig(
        collections=parsed,
        sink=_mapping(config, "sink"),
        workers=_positive_int(config.get("workers", DEFAULT_WORKERS), "workers"),
        retry=_parse_retry(_mapping(config, "retry")),
    )


def build_sink(sink_config: Mapping[str, Any]) -> Sink:
    """Build the configured sink, importing its backend client on demand."""
    kind = str(sink_config.get("type", "")).strip().lower()
    if not kind:
        raise ConfigError("sink.type is required")
    if kind != "influxdb":
        raise ConfigError(f"unsupported sink type {kind!r}; expected 'influxdb'")

    if "token" in sink_config:
        raise ConfigError(
            "sink.token must not appear in the config; name an environment "
            "variable with sink.token_env instead"
        )
    token_env = _required(sink_config, "token_env", "sink")
    token = os.environ.get(str(token_env))
    if not token:
        raise ConfigError(
            f"environment variable {token_env} is unset or empty, so the "
            "sink has no token"
        )

    # Validate every field before importing the backend, so a malformed config
    # is reported the same way whether or not the extra is installed
    settings = {
        "url": str(_required(sink_config, "url", "sink")),
        "org": str(_required(sink_config, "org", "sink")),
        "bucket": str(_required(sink_config, "bucket", "sink")),
        "token": token,
    }
    if "timeout_ms" in sink_config:
        settings["timeout_ms"] = _positive_int(
            sink_config["timeout_ms"], "sink.timeout_ms")

    # Deferred: influxdb-client is an optional extra, so a deployment using a
    # different backend never has to install it
    from .influx import (  # pylint: disable=import-outside-toplevel
        InfluxConfig, InfluxSink,
    )
    return InfluxSink(InfluxConfig(**settings))


def _parse_collection(
    name: str,
    raw: Any,
    defaults: Mapping[str, Any],
) -> CollectionConfig:
    """Validate one entry of the collections section."""
    if not _COLLECTION_NAME.match(name):
        raise ConfigError(
            f"collection name {name!r} must match [a-z0-9_]+, so it composes "
            "into control keyword names"
        )
    if not isinstance(raw, Mapping):
        raise ConfigError(f"collection {name!r} must be a mapping")

    group, daemon = _parse_peer(name, _required(raw, "peer", f"collection {name!r}"))
    interval_s = _positive_float(
        raw.get("interval_s", defaults.get("interval_s", DEFAULT_INTERVAL_S)),
        f"collection {name!r} interval_s")
    timeout_s = _positive_float(
        raw.get("timeout_s", defaults.get("timeout_s", DEFAULT_TIMEOUT_S)),
        f"collection {name!r} timeout_s")

    if interval_s <= TIMEOUT_HEADROOM * timeout_s:
        raise ConfigError(
            f"collection {name!r} has interval_s {interval_s} at or below "
            f"{TIMEOUT_HEADROOM} x timeout_s ({TIMEOUT_HEADROOM * timeout_s}); "
            "one unanswered read would overrun the interval"
        )

    return CollectionConfig(
        name=name,
        group=group,
        daemon=daemon,
        keywords=_patterns(raw.get("keywords"), f"collection {name!r} keywords",
                           required=True),
        exclude=_patterns(raw.get("exclude"), f"collection {name!r} exclude",
                          required=False),
        interval_s=interval_s,
        timeout_s=timeout_s,
        refresh_s=_positive_float(
            raw.get("refresh_s", defaults.get("refresh_s", DEFAULT_REFRESH_S)),
            f"collection {name!r} refresh_s"),
    )


def _parse_retry(raw: Mapping[str, Any]) -> RetryPolicy:
    """Build the retry policy, letting RetryPolicy enforce its own bounds."""
    try:
        return RetryPolicy(
            max_batches=_positive_int(
                raw.get("max_batches", DEFAULT_MAX_BATCHES), "retry.max_batches"),
            **{
                key: _positive_float(raw[key], f"retry.{key}")
                for key in ("base_backoff_s", "max_backoff_s") if key in raw
            },
        )
    except ValueError as exc:
        raise ConfigError(f"retry: {exc}") from exc


def _parse_peer(name: str, raw: Any) -> Tuple[str, str]:
    """Split a collection's peer into group and daemon."""
    parts = str(raw).split(".")
    if len(parts) != 2 or not all(parts) or "%" in str(raw):
        raise ConfigError(
            f"collection {name!r} peer {raw!r} must be '<group>.<daemon>'"
        )
    return parts[0], parts[1]


def _patterns(raw: Any, label: str, *, required: bool) -> Tuple[str, ...]:
    """Validate a list of keyword patterns."""
    if raw is None:
        if required:
            raise ConfigError(f"{label} is required")
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ConfigError(f"{label} must be a list of strings")
    if not all(isinstance(item, str) and item for item in raw):
        raise ConfigError(f"{label} must be a list of non-empty strings")
    if required and not raw:
        raise ConfigError(f"{label} must name at least one pattern")
    return tuple(raw)


def _mapping(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a config section, defaulting to empty."""
    section = config.get(key)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ConfigError(f"{key} must be a mapping")
    return section


def _required(section: Mapping[str, Any], key: str, label: str) -> Any:
    """Return a required value, or raise naming what is missing."""
    value = section.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"{label} is missing {key}")
    return value


def _positive_float(raw: Any, label: str) -> float:
    """Coerce a config value to a positive float."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{label} must be positive, got {value}")
    return value


def _positive_int(raw: Any, label: str) -> int:
    """Coerce a config value to a positive int."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{label} must be an integer, got {raw!r}")
    if raw < 1:
        raise ConfigError(f"{label} must be at least 1, got {raw}")
    return raw


def select_keywords(
    collection: CollectionConfig,
    available: Sequence[str],
) -> Tuple[str, ...]:
    """Return the bare keyword names this collection should read.

    ``available`` is the peer's full keyword list. Includes are applied first,
    then exclusions, so a default exclusion is dropped even when a wildcard
    would otherwise have matched it.
    """
    included: List[str] = []
    for pattern in collection.keywords:
        included.extend(name for name in match_pattern(pattern, available)
                        if name not in included)

    excluded = {name for pattern in collection.excluded()
                for name in match_pattern(pattern, included)}
    return tuple(name for name in included if name not in excluded)
