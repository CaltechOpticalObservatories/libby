"""InfluxDB 2.x sink.

Schema is one measurement per keyword name, tagged by ``group``, ``peer`` and
``units``, with a single ``value`` field. A keyword name carries one type across
peers, so field types stay consistent, and a Grafana query is a measurement
plus a ``peer`` tag filter.

Needs the optional dependency: ``pip install libby[influxdb]``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .sink import Sample, SinkWriteError, Value

# Influx drops an empty tag value, which would split one keyword into two
# series depending on whether it declared units. A literal keeps every point
# on the same series.
NO_UNITS = "none"

DEFAULT_TIMEOUT_MS = 10_000


def field_value(value: Value) -> Value:
    """Coerce a keyword value to the field type Influx should store.

    Ints become floats so that one peer reporting ``0`` and another ``0.5``
    for the same keyword cannot collide as int against float and have the
    write rejected. ``bool`` is checked first, being a subclass of ``int``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)


def to_point(sample: Sample) -> Optional[Point]:
    """Map a sample to a point, or ``None`` if Influx cannot store it.

    A null value is skipped rather than raised: ``lasterror`` is nullable on
    every daemon and is ``None`` most of the time, so a collection reading a
    whole peer would otherwise fail on every tick.
    """
    if sample.value is None:
        return None
    return (
        Point(sample.keyword)
        .tag("group", sample.group)
        .tag("peer", sample.peer)
        .tag("units", sample.units or NO_UNITS)
        .field("value", field_value(sample.value))
        .time(sample.timestamp, WritePrecision.NS)
    )


@dataclass(frozen=True)
class InfluxConfig:
    """Connection settings for one InfluxDB 2.x bucket.

    ``token`` is kept out of ``repr`` so a traceback or a logged config cannot
    leak it.
    """

    url: str
    org: str
    bucket: str
    token: str = field(repr=False)
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class InfluxSink:
    """Writes samples to an InfluxDB 2.x bucket."""

    def __init__(self, config: InfluxConfig) -> None:
        self._config = config
        self._client: Optional[InfluxDBClient] = None
        self._write_api = None

    def connect(self) -> None:
        """Open the client, replacing any existing one."""
        self.close()
        self._client = InfluxDBClient(url=self._config.url,
                                      token=self._config.token,
                                      org=self._config.org,
                                      timeout=self._config.timeout_ms)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def is_connected(self) -> bool:
        """Return whether the server answers a ping."""
        if self._client is None:
            return False
        try:
            return bool(self._client.ping())
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def write(self, samples: Sequence[Sample]) -> int:
        """Write a batch as one request, skipping samples Influx cannot store."""
        if self._write_api is None:
            raise SinkWriteError("influx sink is not connected")
        points = [point for point in map(to_point, samples) if point is not None]
        if not points:
            return 0
        try:
            self._write_api.write(bucket=self._config.bucket,
                                  org=self._config.org, record=points)
        # The client surfaces API, HTTP and socket errors with no common base,
        # and the caller's contract is a single retryable error
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise SinkWriteError(f"influx write failed: {exc}") from exc
        return len(points)

    def close(self) -> None:
        """Release the client; safe to call when never connected."""
        client, self._client = self._client, None
        self._write_api = None
        if client is not None:
            client.close()
