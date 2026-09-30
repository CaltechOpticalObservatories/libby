"""The source contract keyheader reads stored keyword samples from.

A source hands back the same :class:`~libby.keygrabber.sink.Sample` the
keygrabber wrote, so turning measurements, fields, tags or columns back into
samples is the source's job, and a second backend is a new source rather than
a change to keyheader.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Protocol

from ..errors import LibbyError
from ..keygrabber.sink import Sample


class SourceError(LibbyError):
    """A source could not be reached, configured, or read from."""


class SourceReadError(SourceError):
    """A query for samples failed and may be worth retrying."""


class Source(Protocol):
    """Origin of stored samples.

    Implementations own their own schema. ``read`` returns an empty list when
    nothing was stored in the range, and raises :class:`SourceReadError` when
    the query itself failed.
    """

    def connect(self) -> None:
        """Open the connection, or reopen it after a failure."""

    def is_connected(self) -> bool:
        """Return whether the backend is currently reachable."""

    def read(
        self,
        keyword: str,
        start: datetime,
        stop: Optional[datetime] = None,
        peer: Optional[str] = None,
    ) -> List[Sample]:
        """Return one keyword's samples in ``[start, stop)``, oldest first."""

    def close(self) -> None:
        """Release the connection."""
