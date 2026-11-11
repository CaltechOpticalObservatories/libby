"""Turn acquired values into FITS card entries.

An entry is ``{"keyword", "value", "comment"}`` with a JSON value, which the
requesting daemon hands to astropy as a ``(keyword, value, comment)`` tuple.
Keyheader leaves the 80-column layout to astropy rather than writing card
images itself, so the format spec controls a value's precision, not its
column placement.

A value that is missing or unusable becomes ``None``, which astropy writes as
a FITS undefined value: the card is still present, and its comment says why
it is blank.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..keygrabber.sink import Value
from .acquire import Acquired
from .config import MAX_COMMENT, CardConfig, SegmentConfig, ValueType

Entry = Dict[str, Any]


class ValueTypeError(ValueError):
    """A stored value cannot be written as the card's configured type."""


def fits_date(moment: datetime) -> str:
    """Format a moment as a FITS date string in UTC, to the millisecond."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}"


def segment_entries(segment: SegmentConfig, moment: datetime,
                    acquired: List[Acquired]) -> List[Entry]:
    """Build a segment's entries: its COMMENT, its date keyword, then its cards.

    ``acquired`` holds one result per card, in the segment's card order.
    """
    stamp = fits_date(moment)
    entries: List[Entry] = [entry("COMMENT", f"{segment.name} {stamp}", "")]
    if segment.date_keyword is not None:
        entries.append(entry(segment.date_keyword, stamp, segment.date_comment))
    entries.extend(card_entry(card, result)
                   for card, result in zip(segment.cards, acquired))
    return entries


def card_entry(card: CardConfig, acquired: Acquired) -> Entry:
    """Build one card, blank with an explanatory comment if there is no value."""
    if acquired.reason is not None:
        return _blank(card, acquired.reason)
    try:
        return entry(card.fits, to_fits_value(acquired.value, card), card.comment)
    except ValueTypeError as exc:
        return _blank(card, str(exc))


def to_fits_value(value: Value, card: CardConfig) -> Value:
    """Coerce a stored value to the card's type, at the format's precision.

    The format is applied and the result parsed back, so a ``.3f`` float
    carries three decimals of precision while staying a number astropy writes
    as one. InfluxDB stores every integer as a float, so an int card rounds.
    """
    kind = card.type
    if kind is ValueType.BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueTypeError("not a bool")
    if kind is ValueType.STR:
        text = str(value)
        return format(text, card.format) if card.format else text

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueTypeError(f"not a {kind.value}")
    if not math.isfinite(value):
        raise ValueTypeError("not finite")
    if kind is ValueType.INT:
        number = int(round(value))
        return int(format(number, card.format)) if card.format else number
    number = float(value)
    return float(format(number, card.format)) if card.format else number


def entry(keyword: str, value: Value, comment: str) -> Entry:
    """Return one entry in the shape the requesting daemon expects."""
    return {"keyword": keyword, "value": value, "comment": comment}


def _blank(card: CardConfig, reason: str) -> Entry:
    """A card with an undefined value, whose comment leads with the reason."""
    comment = f"{reason}: {card.comment}" if card.comment else reason
    return entry(card.fits, None, comment[:MAX_COMMENT])
