"""Parse and validate keyheader's own configuration sections.

A keyheader config file is an ordinary ``LibbyDaemon`` config (``peer_id``,
``group_id``, ``transport``, ...) plus three sections this module owns:
``source``, ``defaults`` and ``segments``.

Segments and their cards are lists rather than mappings on purpose. YAML keeps
only the last of two identical mapping keys without a word, so a duplicated
FITS keyword or segment name written as a key would vanish instead of being
reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from ..config import ConfigError
# The keygrabber's validators, so both daemons phrase config errors alike
from ..keygrabber.config import (
    _mapping, _positive_float, _positive_int, _required, influx_settings,
)
from .source import Source

DEFAULT_WINDOW_S = 300.0
DEFAULT_WORKERS = 4
DEFAULT_DATE_COMMENT = "UTC time of this segment"

# Value and comment share the 70 columns after ``KEYWORD= ``. A fixed-format
# value takes 20 of them and the ``/`` separator 3, which leaves this much for
# a comment that is never truncated.
MAX_COMMENT = 47

# A COMMENT card's text runs from column 9 to 80, and carries the segment name
# and a timestamp of up to 23 characters
MAX_SEGMENT_NAME = 40

_FITS_KEYWORD = re.compile(r"^[A-Z0-9_-]{1,8}$")
_SEGMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Keywords the FITS 4.0 standard reserves (sections 4.4, 7, 8 and 9), as
# patterns: ``n``/``i``/``j``/``m`` are axis or field numbers and a trailing
# ``a`` is an alternate WCS letter. A mapped keyword may not take any of them,
# because the standard gives each one a meaning or a structural role, and
# COMMENT and HISTORY are written by keyheader itself.
_A = "[A-Z]?"
_N = "[0-9]+"
RESERVED_PATTERNS: Tuple[str, ...] = (
    # Mandatory and structural
    "SIMPLE", "BITPIX", "NAXIS", f"NAXIS{_N}", "END", "XTENSION", "PCOUNT",
    "GCOUNT", "EXTEND", "GROUPS", f"PTYPE{_N}", f"PSCAL{_N}", f"PZERO{_N}",
    "TFIELDS", f"TFORM{_N}", f"TTYPE{_N}", f"TUNIT{_N}", f"TSCAL{_N}",
    f"TZERO{_N}", f"TNULL{_N}", f"TDISP{_N}", f"TDIM{_N}", f"TBCOL{_N}",
    "THEAP", f"TDMIN{_N}", f"TDMAX{_N}", f"TLMIN{_N}", f"TLMAX{_N}",
    # Commentary
    "COMMENT", "HISTORY", "CONTINUE",
    # General descriptive
    "DATE", "ORIGIN", "BLOCKED", "AUTHOR", "REFERENC", "OBSERVER", "TELESCOP",
    "INSTRUME", "OBJECT", "EPOCH", "BUNIT", "BSCALE", "BZERO", "BLANK",
    "DATAMAX", "DATAMIN", "EXTNAME", "EXTVER", "EXTLEVEL", "INHERIT",
    "CHECKSUM", "DATASUM",
    # World coordinates
    f"WCSAXES{_A}", f"WCSNAME{_A}", f"CTYPE{_N}{_A}", f"CUNIT{_N}{_A}",
    f"CRPIX{_N}{_A}", f"CRVAL{_N}{_A}", f"CDELT{_N}{_A}", f"CROTA{_N}",
    f"CNAME{_N}{_A}", f"CRDER{_N}{_A}", f"CSYER{_N}{_A}",
    f"PC{_N}_{_N}{_A}", f"CD{_N}_{_N}{_A}", f"PV{_N}_{_N}{_A}",
    f"PS{_N}_{_N}{_A}", f"LONPOLE{_A}", f"LATPOLE{_A}", f"RADESYS{_A}",
    "RADECSYS", f"EQUINOX{_A}", f"SPECSYS{_A}", f"SSYSOBS{_A}",
    f"SSYSSRC{_A}", f"VELOSYS{_A}", f"ZSOURCE{_A}", f"VELANGL{_A}",
    f"RESTFRQ{_A}", f"RESTWAV{_A}", "RESTFREQ", "OBSGEO-[XYZBLH]",
    # Time
    "DATE-OBS", "DATE-BEG", "DATE-AVG", "DATE-END", "MJD-OBS", "MJD-BEG",
    "MJD-AVG", "MJD-END", "TIMESYS", "MJDREF", "MJDREFI", "MJDREFF", "JDREF",
    "JDREFI", "JDREFF", "DATEREF", "TREFPOS", "TREFDIR", "PLEPHEM",
    "TIMEUNIT", "TIMEOFFS", "TSTART", "TSTOP", "TIMSYER", "TIMRDER",
    "TIMEDEL", "TIMEPIXR", "TELAPSE", "XPOSURE",
)
_RESERVED = re.compile("^(?:" + "|".join(RESERVED_PATTERNS) + ")$")

# The time keywords a segment may name for its own timestamp. They are
# reserved for exactly this meaning, so a segment can use them where a mapped
# keyword cannot.
DATE_KEYWORDS: Tuple[str, ...] = ("DATE-OBS", "DATE-BEG", "DATE-AVG", "DATE-END")


class Mode(str, Enum):
    """How a value is chosen from the samples around the requested time."""

    INTERPOLATE = "interpolate"
    NEAREST = "nearest"
    PRIOR = "prior"
    SUBSEQUENT = "subsequent"


class ValueType(str, Enum):
    """The FITS value type a card is written with."""

    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    STR = "str"


_EXAMPLE_VALUES: Dict[ValueType, Any] = {
    ValueType.FLOAT: 1234.5, ValueType.INT: 1234, ValueType.STR: "text",
}


# A config record, so the field count is the schema rather than complexity
@dataclass(frozen=True)
class CardConfig:  # pylint: disable=too-many-instance-attributes
    """One FITS keyword and the daemon keyword its value comes from."""

    fits: str
    group: str
    peer: str
    keyword: str
    type: ValueType
    format: Optional[str]
    comment: str
    mode: Mode
    window_s: float

    @property
    def source_name(self) -> str:
        """Return the qualified daemon keyword, as written in the config."""
        return f"{self.group}.{self.peer}.{self.keyword}"


@dataclass(frozen=True)
class SegmentConfig:
    """A named set of cards, requested together for one moment."""

    name: str
    date_keyword: Optional[str]
    date_comment: str
    cards: Tuple[CardConfig, ...]


@dataclass(frozen=True)
class KeyheaderConfig:
    """Keyheader's parsed configuration."""

    segments: Tuple[SegmentConfig, ...]
    source: Mapping[str, Any]
    workers: int

    def segment(self, name: str) -> SegmentConfig:
        """Return a segment by name, or raise naming the ones that exist."""
        for segment in self.segments:
            if segment.name == name:
                return segment
        known = ", ".join(segment.name for segment in self.segments)
        raise ValueError(f"unknown segment {name!r}; configured: {known}")


def parse_config(config: Mapping[str, Any]) -> KeyheaderConfig:
    """Validate a daemon config's keyheader sections into typed objects."""
    defaults = _mapping(config, "defaults")
    raw_segments = config.get("segments")
    if isinstance(raw_segments, Mapping):
        raise ConfigError(
            "segments must be a list of entries with a name, not a mapping: "
            "YAML silently drops a repeated mapping key, which would hide a "
            "duplicate segment")
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, str) \
            or not raw_segments:
        raise ConfigError("segments must be a non-empty list")

    segments = tuple(_parse_segment(index, raw, defaults)
                     for index, raw in enumerate(raw_segments))
    _check_unique(segments)
    return KeyheaderConfig(
        segments=segments,
        source=_mapping(config, "source"),
        workers=_positive_int(config.get("workers", DEFAULT_WORKERS), "workers"),
    )


def build_source(source_config: Mapping[str, Any]) -> Source:
    """Build the configured source, importing its backend client on demand."""
    settings = influx_settings(source_config, "source")
    # Deferred: influxdb-client is an optional extra
    from ..keygrabber.influx import (  # pylint: disable=import-outside-toplevel
        InfluxConfig, InfluxSource,
    )
    return InfluxSource(InfluxConfig(**settings))


def is_reserved(keyword: str) -> bool:
    """Return whether the FITS standard reserves this keyword."""
    return bool(_RESERVED.match(keyword))


def _parse_segment(index: int, raw: Any,
                   defaults: Mapping[str, Any]) -> SegmentConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"segments[{index}] must be a mapping")
    name = str(_required(raw, "name", f"segments[{index}]"))
    if not _SEGMENT_NAME.match(name) or len(name) > MAX_SEGMENT_NAME:
        raise ConfigError(
            f"segment name {name!r} must be letters, digits, '_', '-' or '.', "
            f"at most {MAX_SEGMENT_NAME} characters, so it fits a COMMENT card")
    label = f"segment {name!r}"

    date_keyword = raw.get("date_keyword")
    if date_keyword is not None:
        date_keyword = str(date_keyword)
        if date_keyword not in DATE_KEYWORDS:
            raise ConfigError(
                f"{label} date_keyword {date_keyword!r} must be one of "
                f"{', '.join(DATE_KEYWORDS)}")
    date_comment = _comment(
        raw.get("date_comment", DEFAULT_DATE_COMMENT), f"{label} date_comment")

    raw_cards = raw.get("cards")
    if isinstance(raw_cards, Mapping):
        raise ConfigError(
            f"{label} cards must be a list of entries with a fits name, not a "
            "mapping: YAML silently drops a repeated mapping key, which would "
            "hide a duplicate keyword")
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, str) \
            or not raw_cards:
        raise ConfigError(f"{label} cards must be a non-empty list")

    defaults = {**defaults, **_mapping(raw, "defaults")}
    cards = tuple(_parse_card(f"{label} cards[{position}]", card, defaults)
                  for position, card in enumerate(raw_cards))
    return SegmentConfig(name=name, date_keyword=date_keyword,
                         date_comment=date_comment, cards=cards)


def _parse_card(label: str, raw: Any, defaults: Mapping[str, Any]) -> CardConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{label} must be a mapping")

    fits = str(_required(raw, "fits", label))
    if not _FITS_KEYWORD.match(fits):
        raise ConfigError(
            f"{label} FITS keyword {fits!r} must be 1-8 characters of A-Z, "
            "0-9, '-' or '_'")
    label = f"{label} ({fits})"
    if is_reserved(fits):
        raise ConfigError(
            f"{label} is reserved by the FITS standard and cannot be mapped")

    group, peer, keyword = _parse_source_name(
        label, _required(raw, "keyword", label))
    value_type = _enum(ValueType, _required(raw, "type", label), f"{label} type")
    mode = _enum(Mode, raw.get("mode", defaults.get("mode", Mode.NEAREST.value)),
                 f"{label} mode")
    if mode is Mode.INTERPOLATE and value_type in (ValueType.BOOL, ValueType.STR):
        raise ConfigError(
            f"{label} cannot interpolate a {value_type.value} value; use "
            "nearest, prior or subsequent")

    return CardConfig(
        fits=fits,
        group=group,
        peer=peer,
        keyword=keyword,
        type=value_type,
        format=_format(raw.get("format"), value_type, label),
        comment=_comment(raw.get("comment", ""), f"{label} comment"),
        mode=mode,
        window_s=_positive_float(
            raw.get("window_s", defaults.get("window_s", DEFAULT_WINDOW_S)),
            f"{label} window_s"),
    )


def _check_unique(segments: Sequence[SegmentConfig]) -> None:
    """Reject a repeated segment name, or a FITS keyword used twice in a segment.

    Keywords need only be unique within a segment. Several camera daemons can
    each have their own segment using the same keywords, so two segments may
    share a name; a daemon that adds two segments to one header must choose
    segments that do not overlap.
    """
    seen_segments: Dict[str, int] = {}
    for index, segment in enumerate(segments):
        if segment.name in seen_segments:
            raise ConfigError(
                f"segment name {segment.name!r} appears twice "
                f"(segments[{seen_segments[segment.name]}] and "
                f"segments[{index}])")
        seen_segments[segment.name] = index

    for segment in segments:
        seen: Set[str] = set()
        names = [card.fits for card in segment.cards]
        if segment.date_keyword is not None:
            names.insert(0, segment.date_keyword)
        for name in names:
            if name in seen:
                raise ConfigError(f"FITS keyword {name} appears twice in "
                                  f"segment {segment.name!r}")
            seen.add(name)


def _parse_source_name(label: str, raw: Any) -> Tuple[str, str, str]:
    """Split ``<group>.<daemon>.<keyword>`` into its parts."""
    text = str(raw)
    parts = text.split(".", 2)
    if len(parts) != 3 or not all(parts) or "%" in text:
        raise ConfigError(
            f"{label} keyword {text!r} must be '<group>.<daemon>.<keyword>'")
    return parts[0], parts[1], parts[2]


def _format(raw: Any, value_type: ValueType, label: str) -> Optional[str]:
    """Validate a Python format spec against the card's type."""
    if raw is None:
        return None
    spec = str(raw)
    if value_type is ValueType.BOOL:
        raise ConfigError(f"{label} format does not apply to a bool, which "
                          "FITS always writes as T or F")
    try:
        text = format(_EXAMPLE_VALUES[value_type], spec)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"{label} format {spec!r} is not valid for a "
            f"{value_type.value}: {exc}") from exc
    # A number is formatted and parsed back to set its precision, so a spec
    # that adds separators or padding text would fail on every request
    if value_type is not ValueType.STR:
        try:
            (float if value_type is ValueType.FLOAT else int)(text)
        except ValueError:
            raise ConfigError(
                f"{label} format {spec!r} gives {text!r}, which does not read "
                f"back as a {value_type.value}") from None
    return spec


def _comment(raw: Any, label: str) -> str:
    text = "" if raw is None else str(raw)
    if len(text) > MAX_COMMENT:
        raise ConfigError(
            f"{label} is {len(text)} characters; at most {MAX_COMMENT} fit on "
            "the card without truncation")
    if not text.isascii() or not text.isprintable():
        raise ConfigError(f"{label} must be printable ASCII")
    return text


def _enum(kind: Any, raw: Any, label: str) -> Any:
    try:
        return kind(str(raw).strip().lower())
    except ValueError:
        choices = ", ".join(member.value for member in kind)
        raise ConfigError(
            f"{label} {raw!r} must be one of {choices}") from None
