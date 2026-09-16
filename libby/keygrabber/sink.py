"""The sample the keygrabber collects, and the sink contract it writes to.

A ``Sample`` carries nothing backend-specific: turning one into measurements,
fields, tags or columns is the sink's job, so a second backend is a new sink
rather than a change to the collector.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Deque, Optional, Protocol, Sequence, Tuple, Union

from ..errors import LibbyError

Value = Union[bool, int, float, str, None]

DEFAULT_MAX_BATCHES = 64
DEFAULT_BASE_BACKOFF_S = 1.0
DEFAULT_MAX_BACKOFF_S = 60.0


class SinkError(LibbyError):
    """A sink could not be reached, configured, or written to."""


class SinkWriteError(SinkError):
    """A batch of samples could not be written and may be worth retrying."""


@dataclass(frozen=True)
class Sample:
    """One keyword's value, read at one instant."""

    keyword: str
    group: str
    peer: str
    value: Value
    units: Optional[str]
    timestamp: datetime


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds on how a :class:`RetryingWriter` queues and re-attempts batches."""

    max_batches: int = DEFAULT_MAX_BATCHES
    base_backoff_s: float = DEFAULT_BASE_BACKOFF_S
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S

    def __post_init__(self) -> None:
        if self.max_batches < 1:
            raise ValueError("max_batches must be at least 1")
        if self.base_backoff_s <= 0:
            raise ValueError("base_backoff_s must be positive")


class Sink(Protocol):
    """Destination for collected samples.

    Implementations own their own schema. ``write`` returns how many samples it
    actually stored, which can be fewer than it was given when a backend cannot
    represent some of them, and raises :class:`SinkWriteError` when the batch
    failed and should be retried.
    """

    def connect(self) -> None:
        """Open the connection, or reopen it after a failure."""

    def is_connected(self) -> bool:
        """Return whether the backend is currently reachable."""

    def write(self, samples: Sequence[Sample]) -> int:
        """Store a batch and return how many samples were written."""

    def close(self) -> None:
        """Release the connection."""


class RetryingWriter:
    """Wraps a sink, holding failed batches in a bounded queue for retry.

    Retrying is backend-independent, so it lives here rather than inside any
    one sink. The queue is bounded and drops its oldest batch when full, so a
    database that stays down cannot grow the daemon's memory without limit.

    Nothing here starts a thread and nothing sleeps: the caller decides when to
    call :meth:`flush_due`, and ``clock`` is injectable so backoff is testable
    without waiting for it.
    """

    def __init__(
        self,
        sink: Sink,
        *,
        policy: Optional[RetryPolicy] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._policy = policy or RetryPolicy()
        self._clock = clock
        self._pending: Deque[Tuple[Sample, ...]] = deque()
        self._failures = 0
        self._retry_at = 0.0
        self._dropped_batches = 0

    @property
    def queue_depth(self) -> int:
        """Number of batches waiting to be retried."""
        return len(self._pending)

    @property
    def dropped_batches(self) -> int:
        """Number of batches discarded because the queue was full."""
        return self._dropped_batches

    def write(self, samples: Sequence[Sample]) -> int:
        """Write a batch now, or queue it if the sink is in backoff."""
        batch = tuple(samples)
        if not batch:
            return 0
        # Queue behind an existing backlog rather than overtaking it, and do
        # not probe a sink that is still inside its backoff window
        if self._pending or not self._backoff_elapsed():
            self._enqueue(batch)
            return 0
        return self._attempt(batch)

    def flush_due(self) -> int:
        """Retry queued batches, oldest first, once backoff has elapsed."""
        if not self._pending or not self._backoff_elapsed():
            return 0
        written = 0
        while self._pending:
            try:
                written += self._sink.write(self._pending[0])
            except SinkWriteError:
                self._arm_backoff()
                return written
            self._pending.popleft()
        self._reset_backoff()
        return written

    def connect(self) -> None:
        """Reconnect the wrapped sink and clear its backoff."""
        self._sink.connect()
        self._reset_backoff()

    def is_connected(self) -> bool:
        """Return whether the wrapped sink is reachable."""
        return self._sink.is_connected()

    def close(self) -> None:
        """Release the wrapped sink."""
        self._sink.close()

    def _attempt(self, batch: Tuple[Sample, ...]) -> int:
        try:
            written = self._sink.write(batch)
        except SinkWriteError:
            self._enqueue(batch)
            self._arm_backoff()
            return 0
        self._reset_backoff()
        return written

    def _enqueue(self, batch: Tuple[Sample, ...]) -> None:
        if len(self._pending) >= self._policy.max_batches:
            self._pending.popleft()
            self._dropped_batches += 1
        self._pending.append(batch)

    def _backoff_elapsed(self) -> bool:
        return self._clock() >= self._retry_at

    def _arm_backoff(self) -> None:
        self._failures += 1
        delay = min(self._policy.base_backoff_s * (2 ** (self._failures - 1)),
                    self._policy.max_backoff_s)
        self._retry_at = self._clock() + delay

    def _reset_backoff(self) -> None:
        self._failures = 0
        self._retry_at = 0.0
