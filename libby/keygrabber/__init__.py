"""Keygrabber: poll keywords from libby peers and store them as time series.

Importing this package does not pull in any database client. ``InfluxSink``
lives in :mod:`libby.keygrabber.influx` and needs the optional dependency
(``pip install libby[influxdb]``), so a deployment that only uses another
backend never has to install it.
"""
from .collection import Collection, TickResult
from .config import (
    CollectionConfig,
    KeygrabberConfig,
    build_sink,
    parse_config,
    select_keywords,
)
from .daemon import KeygrabberDaemon
from .sink import (
    RetryingWriter,
    RetryPolicy,
    Sample,
    Sink,
    SinkError,
    SinkWriteError,
)

__all__ = [
    "Collection",
    "CollectionConfig",
    "KeygrabberConfig",
    "KeygrabberDaemon",
    "RetryingWriter",
    "RetryPolicy",
    "Sample",
    "Sink",
    "SinkError",
    "SinkWriteError",
    "TickResult",
    "build_sink",
    "parse_config",
    "select_keywords",
]
