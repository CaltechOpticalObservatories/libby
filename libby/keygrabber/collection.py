"""One configured peer, resolved against the live peer and read on a cadence."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..client import Client
from ..errors import LibbyError, LibbyTimeout
from .config import CollectionConfig, select_keywords
from .sink import Sample

BULK_READ_SERVICE = "keys.read"

# Failures tolerated at the configured cadence before a collection starts
# reading less often. A peer restarting should not trigger a backoff.
FAILURES_BEFORE_BACKOFF = 3

# Ceiling on how far the interval is stretched while a peer stays silent, so a
# recovered peer is picked up again within a bounded time
MAX_BACKOFF_MULTIPLIER = 32


@dataclass(frozen=True)
class TickResult:
    """What one read of a collection produced."""

    samples: Tuple[Sample, ...]
    read_errors: int


# Config plus the runtime state the control keywords expose; each attribute is
# one reported value rather than hidden complexity
class Collection:  # pylint: disable=too-many-instance-attributes
    """Tracks what one peer exposes and turns a read of it into samples.

    Resolution is refreshed periodically rather than once, so keywords added by
    a restarted daemon are picked up without restarting the keygrabber.
    """

    def __init__(
        self,
        config: CollectionConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        # Runtime state the control keywords read and write. Plain attributes
        # rather than lock-guarded: each is a single value written by one
        # thread and read by the transport's receive thread, and that thread
        # must never block on a lock a tick might hold.
        self.enabled = True
        self.last_sample: Optional[datetime] = None
        self.lag_s = 0.0
        self.consecutive_failures = 0
        self._clock = clock
        self._names: Tuple[str, ...] = ()
        self._bulk_read = False
        self._resolved_at: Optional[float] = None

    @property
    def name(self) -> str:
        """Return the collection's configured name."""
        return self.config.name

    @property
    def keyword_count(self) -> int:
        """Return how many keywords the last resolve selected."""
        return len(self._names)

    @property
    def bulk_read(self) -> bool:
        """Return whether the peer advertised the bulk read service."""
        return self._bulk_read

    def needs_resolve(self) -> bool:
        """Return whether the keyword selection is due to be refreshed."""
        if self._resolved_at is None:
            return True
        return self._clock() - self._resolved_at >= self.config.refresh_s

    def invalidate(self) -> None:
        """Force the next tick to resolve again, after a config change."""
        self._resolved_at = None

    def note_failure(self) -> None:
        """Record a tick that read nothing."""
        self.consecutive_failures += 1

    def note_success(self) -> None:
        """Record a tick that read something, ending any backoff."""
        self.consecutive_failures = 0

    def backoff_interval_s(self) -> float:
        """Return the interval to use next, stretched while the peer fails.

        A peer that is down would otherwise be retried, and logged about, on
        its configured cadence indefinitely. Backing off keeps a dead daemon
        from dominating both the logs and the read budget, while the cap keeps
        a recovered one from waiting long to be noticed.
        """
        if self.consecutive_failures < FAILURES_BEFORE_BACKOFF:
            return self.config.interval_s
        overshoot = self.consecutive_failures - FAILURES_BEFORE_BACKOFF + 1
        return self.config.interval_s * min(2 ** overshoot, MAX_BACKOFF_MULTIPLIER)

    def resolve(self, client: Client) -> Tuple[str, ...]:
        """Ask the peer what it serves and select the configured keywords.

        One ``keys.list`` covers both: the peer's keyword names, and whether it
        serves ``keys.read``. Selection is then local, so a collection with
        several patterns still costs one request.
        """
        listing = client.listing(f"{self.config.peer}.%",
                                 timeout_s=self.config.timeout_s)
        available = [name.rsplit(".", 1)[-1] for name in listing.names]
        self._names = select_keywords(self.config, available)
        self._bulk_read = BULK_READ_SERVICE in listing.services
        self._resolved_at = self._clock()
        return self._names

    def tick(self, client: Client, timestamp: datetime) -> TickResult:
        """Read every selected keyword once and return the samples."""
        if not self._names:
            return TickResult((), 0)

        qualified = [f"{self.config.peer}.{name}" for name in self._names]
        responses = (
            client.read(qualified, timeout_s=self.config.timeout_s)
            if self._bulk_read
            else self._read_individually(client, qualified)
        )

        samples: List[Sample] = []
        read_errors = 0
        for qualified_name, response in responses.items():
            if not response.get("ok"):
                read_errors += 1
                continue
            samples.append(self._sample(qualified_name, response, timestamp))
        return TickResult(tuple(samples), read_errors)

    def _read_individually(
        self,
        client: Client,
        qualified: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Read one keyword at a time, for a peer without ``keys.read``.

        Abandons the rest of the tick after the first timeout: a peer that has
        stopped answering would otherwise cost ``timeout_s`` per keyword and
        overrun the interval many times over.
        """
        responses: Dict[str, Dict[str, Any]] = {}
        timed_out = False
        for name in qualified:
            if timed_out:
                responses[name] = {"ok": False, "error": "skipped after timeout"}
                continue
            try:
                responses[name] = client.show(name, timeout_s=self.config.timeout_s)
            except LibbyTimeout as exc:
                timed_out = True
                responses[name] = {"ok": False, "error": str(exc)}
            except LibbyError as exc:
                responses[name] = {"ok": False, "error": str(exc)}
        return responses

    def _sample(
        self,
        qualified_name: str,
        response: Dict[str, Any],
        timestamp: datetime,
    ) -> Sample:
        """Build a sample from one keyword response.

        A null value is kept rather than dropped here: whether it can be stored
        is the sink's business, and a backend other than Influx may hold it.
        """
        return Sample(
            keyword=qualified_name.rsplit(".", 1)[-1],
            group=self.config.group,
            peer=self.config.daemon,
            value=response.get("value"),
            units=response.get("units"),
            timestamp=timestamp,
        )
