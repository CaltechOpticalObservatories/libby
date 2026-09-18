"""Unit tests for a collection backing off while its peer stays silent."""
from __future__ import annotations

import unittest

from libby.keygrabber import Collection
from libby.keygrabber.collection import (
    FAILURES_BEFORE_BACKOFF,
    MAX_BACKOFF_MULTIPLIER,
)
from libby.keygrabber.config import CollectionConfig
from libby.keygrabber.scheduler import Scheduler

INTERVAL_S = 10.0


def _collection() -> Collection:
    return Collection(CollectionConfig(
        name="adc", group="hsfei", daemon="adc", keywords=("%",), exclude=(),
        interval_s=INTERVAL_S, timeout_s=1.0, refresh_s=300.0))


class _FakeClock:
    """Manually advanced clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class BackoffIntervalTests(unittest.TestCase):
    """How a collection stretches its own cadence while failing."""

    def setUp(self) -> None:
        self.collection = _collection()

    def test_a_healthy_collection_uses_its_configured_cadence(self):
        """Leave the interval alone when nothing has failed."""
        self.assertEqual(self.collection.backoff_interval_s(), INTERVAL_S)

    def test_early_failures_do_not_slow_it_down(self):
        """Tolerate a peer restarting without changing its cadence."""
        for _ in range(FAILURES_BEFORE_BACKOFF - 1):
            self.collection.note_failure()
        self.assertEqual(self.collection.backoff_interval_s(), INTERVAL_S)

    def test_the_interval_grows_once_failures_persist(self):
        """Read a persistently dead peer less often."""
        for _ in range(FAILURES_BEFORE_BACKOFF):
            self.collection.note_failure()
        first = self.collection.backoff_interval_s()
        self.assertGreater(first, INTERVAL_S)

        self.collection.note_failure()
        self.assertGreater(self.collection.backoff_interval_s(), first)

    def test_the_interval_is_capped(self):
        """Keep a recovered peer from waiting indefinitely to be noticed."""
        for _ in range(100):
            self.collection.note_failure()
        self.assertEqual(self.collection.backoff_interval_s(),
                         INTERVAL_S * MAX_BACKOFF_MULTIPLIER)

    def test_one_success_clears_the_backoff(self):
        """Return to the configured cadence as soon as a read works."""
        for _ in range(20):
            self.collection.note_failure()
        self.collection.note_success()
        self.assertEqual(self.collection.consecutive_failures, 0)
        self.assertEqual(self.collection.backoff_interval_s(), INTERVAL_S)


class SchedulerHonoursBackoffTests(unittest.TestCase):
    """The scheduler reads the stretched interval, not the configured one."""

    def test_a_failing_collection_is_scheduled_further_out(self):
        """Space out the next tick of a collection that keeps failing."""
        clock = _FakeClock()
        scheduler = Scheduler(clock=clock)
        collection = _collection()
        scheduler.replace([collection])

        scheduler.claim_due()
        scheduler.release(collection.name)
        self.assertEqual(scheduler.next_delay(), INTERVAL_S)

        for _ in range(FAILURES_BEFORE_BACKOFF):
            collection.note_failure()
        clock.advance(INTERVAL_S)
        scheduler.claim_due()
        scheduler.release(collection.name)
        self.assertGreater(scheduler.next_delay(), INTERVAL_S)

    def test_recovery_restores_the_configured_cadence(self):
        """Go back to the normal interval on the first successful read."""
        clock = _FakeClock()
        scheduler = Scheduler(clock=clock)
        collection = _collection()
        scheduler.replace([collection])

        for _ in range(FAILURES_BEFORE_BACKOFF + 2):
            collection.note_failure()
        scheduler.claim_due()
        scheduler.release(collection.name)
        self.assertGreater(scheduler.next_delay(), INTERVAL_S)

        collection.note_success()
        clock.advance(scheduler.next_delay())
        scheduler.claim_due()
        scheduler.release(collection.name)
        self.assertEqual(scheduler.next_delay(), INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
