"""Keyheader: answer FITS header segments from keyword values the keygrabber stored.

A requesting daemon (usually a camera) asks for a named segment at a moment,
and gets back ``{keyword, value, comment}`` entries to add to its image header.

Importing this package does not pull in any database client. ``InfluxSource``
lives in :mod:`libby.keygrabber.influx` and needs the optional dependency
(``pip install libby[influxdb]``).
"""
from .acquire import Acquired, acquire, query_range
from .cards import card_entry, fits_date, segment_entries, to_fits_value
from .config import (
    CardConfig,
    KeyheaderConfig,
    Mode,
    SegmentConfig,
    ValueType,
    build_source,
    is_reserved,
    parse_config,
)
from .daemon import SERVICE_KEY, KeyheaderDaemon, parse_time
from .source import Source, SourceError, SourceReadError

__all__ = [
    "Acquired",
    "CardConfig",
    "KeyheaderConfig",
    "KeyheaderDaemon",
    "Mode",
    "SERVICE_KEY",
    "SegmentConfig",
    "Source",
    "SourceError",
    "SourceReadError",
    "ValueType",
    "acquire",
    "build_source",
    "card_entry",
    "fits_date",
    "is_reserved",
    "parse_config",
    "parse_time",
    "query_range",
    "segment_entries",
    "to_fits_value",
]
