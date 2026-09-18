"""Decides which collections are due to be read, and which to skip.

Deliberately free of threads, pools and sleeping: it answers "what should run
now" and nothing else, so the due-time ordering and the skip rule can be tested
against an injected clock rather than by racing real threads.
"""
from __future__ import annotations

import heapq
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .collection import Collection


class Scheduler:
    """A due-time heap over collections, with at most one tick each in flight.

    Every method takes the lock only for in-memory bookkeeping, never across a
    read or a write, because ``reload`` calls :meth:`replace` from the
    transport's receive thread and blocking that would time out every read
    already in flight.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._collections: Dict[str, Collection] = {}
        self._due: List[Tuple[float, str]] = []
        self._in_flight: Set[str] = set()

    def replace(self, collections: Iterable[Collection]) -> None:
        """Swap the scheduled set, leaving in-flight ticks to finish.

        Every new collection is due immediately, so a reload takes effect on
        the next pass rather than after a full interval.
        """
        now = self._clock()
        with self._lock:
            self._collections = {item.name: item for item in collections}
            self._due = [(now, name) for name in self._collections]
            heapq.heapify(self._due)
            # Names no longer scheduled are dropped; a tick still running for
            # one releases harmlessly below
            self._in_flight &= set(self._collections)

    def next_delay(self) -> Optional[float]:
        """Return seconds until the next tick, or None when nothing is scheduled."""
        with self._lock:
            if not self._due:
                return None
            return max(0.0, self._due[0][0] - self._clock())

    def claim_due(self) -> Tuple[Tuple[Collection, ...], int]:
        """Claim every collection now due, and count those already running.

        Returns the collections the caller should tick, and how many ticks were
        skipped because their predecessor had not finished. A skipped tick is
        not retried sooner: it waits for its next interval, so a slow peer
        settles at a lower rate instead of building a backlog.
        """
        claimed: List[Collection] = []
        skipped = 0
        with self._lock:
            now = self._clock()
            while self._due and self._due[0][0] <= now:
                due_at, name = heapq.heappop(self._due)
                collection = self._collections.get(name)
                if collection is None:
                    continue    # dropped by a reload; stop scheduling it
                if name in self._in_flight:
                    skipped += 1
                else:
                    self._in_flight.add(name)
                    collection.lag_s = max(0.0, now - due_at)
                    claimed.append(collection)
                # Rescheduled from now, not from when it was due, so a long
                # stall cannot leave a burst of catch-up ticks that can only
                # skip. Cadence drifts by the loop's own latency instead, and
                # stretches while the peer is failing.
                heapq.heappush(
                    self._due, (now + collection.backoff_interval_s(), name))
        return tuple(claimed), skipped

    def update(self, collection: Collection) -> None:
        """Replace one collection and reschedule only it.

        Used when a control keyword changes a cadence, so adjusting one
        collection does not re-seed every other collection's due time and set
        the whole fleet reading at once.
        """
        with self._lock:
            self._collections[collection.name] = collection
            self._due = [entry for entry in self._due
                         if entry[1] != collection.name]
            heapq.heapify(self._due)
            heapq.heappush(self._due, (self._clock(), collection.name))

    def release(self, name: str) -> None:
        """Mark a collection's tick finished, so its next one may run."""
        with self._lock:
            self._in_flight.discard(name)

    @property
    def in_flight(self) -> int:
        """Number of ticks currently running."""
        with self._lock:
            return len(self._in_flight)

    @property
    def scheduled(self) -> Tuple[str, ...]:
        """Names currently scheduled, in no particular order."""
        with self._lock:
            return tuple(self._collections)
