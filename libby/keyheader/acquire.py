"""Choose a keyword's value at one moment from the samples stored around it.

Pure functions over already-fetched samples, so every acquisition mode can be
tested without a database. :func:`query_range` says what to fetch and
:func:`acquire` picks from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from numbers import Real
from typing import Optional, Sequence, Tuple

from ..keygrabber.sink import Sample, Value
from .config import Mode

# A source reads ``[start, stop)``, so a sample stamped exactly at the
# requested moment needs the range to run just past it
_INCLUSIVE = timedelta(microseconds=1)


@dataclass(frozen=True)
class Acquired:
    """A chosen value, or the reason there is none.

    ``reason`` is set exactly when ``value`` is None, and is short enough to
    put on the card in place of the value.
    """

    value: Value
    reason: Optional[str] = None

    @classmethod
    def missing(cls, reason: str) -> "Acquired":
        """Return a result with no value, saying why."""
        return cls(None, reason)


def query_range(moment: datetime, mode: Mode,
                window_s: float) -> Tuple[datetime, datetime]:
    """Return the ``[start, stop)`` range a mode needs samples from."""
    window = timedelta(seconds=window_s)
    if mode is Mode.PRIOR:
        return moment - window, moment + _INCLUSIVE
    if mode is Mode.SUBSEQUENT:
        return moment, moment + window
    return moment - window, moment + window


def acquire(samples: Sequence[Sample], moment: datetime, mode: Mode) -> Acquired:
    """Pick the value ``mode`` asks for from samples sorted oldest first.

    Null values are ignored, since they say nothing about the keyword's state.
    """
    usable = [sample for sample in samples if sample.value is not None]
    before = _last(sample for sample in usable if sample.timestamp <= moment)
    after = _first(sample for sample in usable if sample.timestamp >= moment)

    if mode is Mode.PRIOR:
        return _from(before, "no prior sample")
    if mode is Mode.SUBSEQUENT:
        return _from(after, "no later sample")
    if mode is Mode.NEAREST:
        return _from(_closer(before, after, moment), "no sample")
    return _interpolate(before, after, moment)


def _interpolate(before: Optional[Sample], after: Optional[Sample],
                 moment: datetime) -> Acquired:
    """Interpolate linearly, or fall back to the one side that exists.

    A request made at the end of an exposure usually comes before the next
    sample is stored. Most values are static across an exposure, so the last
    stored one stands in rather than leaving the card blank.
    """
    if before is None or after is None:
        return _from(before or after, "no sample")
    if before.timestamp == after.timestamp:
        return Acquired(before.value)
    if not (_is_number(before.value) and _is_number(after.value)):
        return Acquired.missing("not numeric")
    span = (after.timestamp - before.timestamp).total_seconds()
    fraction = (moment - before.timestamp).total_seconds() / span
    low, high = float(before.value), float(after.value)  # type: ignore[arg-type]
    return Acquired(low + (high - low) * fraction)


def _closer(before: Optional[Sample], after: Optional[Sample],
            moment: datetime) -> Optional[Sample]:
    """Return whichever sample is nearer in time, preferring the earlier on a tie."""
    if before is None or after is None:
        return before or after
    if (after.timestamp - moment) < (moment - before.timestamp):
        return after
    return before


def _from(sample: Optional[Sample], reason: str) -> Acquired:
    return Acquired.missing(reason) if sample is None else Acquired(sample.value)


def _is_number(value: Value) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _first(samples) -> Optional[Sample]:
    return next(iter(samples), None)


def _last(samples) -> Optional[Sample]:
    found = None
    for found in samples:
        pass
    return found
