"""Unit tests for Scheduler: due ordering, the skip rule and rescheduling.

The clock is injected, so none of this waits for a real interval.
"""
from __future__ import annotations

import dataclasses
import unittest

from libby.keygrabber import Collection
from libby.keygrabber.config import CollectionConfig
from libby.keygrabber.scheduler import Scheduler


def _collection(name: str, interval_s: float = 10.0) -> Collection:
    return Collection(CollectionConfig(
        name=name, group="hsfei", daemon=name, keywords=("%",), exclude=(),
        interval_s=interval_s, timeout_s=1.0, refresh_s=300.0))


class _FakeClock:
    """Manually advanced clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class SchedulerTests(unittest.TestCase):
    """What the scheduler decides to run, and when."""

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.scheduler = Scheduler(clock=self.clock)

    def _names(self, claimed) -> list:
        return sorted(collection.name for collection in claimed)

    def test_nothing_scheduled_is_quiet(self):
        """Report no work and no delay before anything is scheduled."""
        self.assertIsNone(self.scheduler.next_delay())
        self.assertEqual(self.scheduler.claim_due(), ((), 0))

    def test_everything_is_due_immediately_after_replace(self):
        """Start collecting at once rather than after a first full interval."""
        self.scheduler.replace([_collection("adc"), _collection("pressure")])
        self.assertEqual(self.scheduler.next_delay(), 0.0)
        claimed, skipped = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["adc", "pressure"])
        self.assertEqual(skipped, 0)

    def test_nothing_is_due_again_until_the_interval_passes(self):
        """Hold a collection for its interval once it has run."""
        self.scheduler.replace([_collection("adc", interval_s=10.0)])
        self.scheduler.claim_due()
        self.scheduler.release("adc")

        self.clock.advance(9.0)
        self.assertEqual(self.scheduler.claim_due(), ((), 0))
        self.clock.advance(1.5)
        claimed, _ = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["adc"])

    def test_shorter_interval_comes_due_first(self):
        """Order by due time, not by insertion."""
        self.scheduler.replace([_collection("slow", interval_s=60.0),
                                _collection("fast", interval_s=1.0)])
        self.scheduler.claim_due()
        self.scheduler.release("slow")
        self.scheduler.release("fast")

        self.clock.advance(2.0)
        claimed, _ = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["fast"])

    def test_tick_is_skipped_while_its_predecessor_runs(self):
        """Count a skip rather than letting a slow peer overlap itself."""
        self.scheduler.replace([_collection("adc", interval_s=1.0)])
        self.scheduler.claim_due()          # claimed, never released

        self.clock.advance(1.5)
        claimed, skipped = self.scheduler.claim_due()
        self.assertEqual(claimed, ())
        self.assertEqual(skipped, 1)
        self.assertEqual(self.scheduler.in_flight, 1)

    def test_release_lets_the_next_tick_run(self):
        """Resume a collection once its tick finishes."""
        self.scheduler.replace([_collection("adc", interval_s=1.0)])
        self.scheduler.claim_due()
        self.scheduler.release("adc")
        self.assertEqual(self.scheduler.in_flight, 0)

        self.clock.advance(1.5)
        claimed, skipped = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["adc"])
        self.assertEqual(skipped, 0)

    def test_lag_records_how_late_a_tick_started(self):
        """Report lateness, so a fleet that cannot keep up is visible."""
        collection = _collection("adc", interval_s=1.0)
        self.scheduler.replace([collection])
        self.clock.advance(4.0)
        self.scheduler.claim_due()
        self.assertEqual(collection.lag_s, 4.0)

    def test_a_long_stall_does_not_queue_catch_up_ticks(self):
        """Reschedule from now, so a stall cannot leave a burst that can only skip."""
        self.scheduler.replace([_collection("adc", interval_s=1.0)])
        self.scheduler.claim_due()
        self.scheduler.release("adc")

        self.clock.advance(60.0)             # a minute of stall on a 1s cadence
        claimed, skipped = self.scheduler.claim_due()
        self.assertEqual(len(claimed), 1)    # one tick, not sixty
        self.assertEqual(skipped, 0)
        self.scheduler.release("adc")
        self.assertEqual(self.scheduler.next_delay(), 1.0)

    def test_update_reschedules_only_its_own_collection(self):
        """Change one cadence without re-seeding every other due time."""
        adc = _collection("adc", interval_s=10.0)
        pressure = _collection("pressure", interval_s=10.0)
        self.scheduler.replace([adc, pressure])
        self.scheduler.claim_due()
        self.scheduler.release("adc")
        self.scheduler.release("pressure")

        adc.config = dataclasses.replace(adc.config, interval_s=1.0)
        self.scheduler.update(adc)

        claimed, _ = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["adc"])   # pressure still waits

    def test_replace_stops_scheduling_a_removed_collection(self):
        """Drop a collection a reload removed."""
        self.scheduler.replace([_collection("adc"), _collection("pressure")])
        self.scheduler.claim_due()
        self.scheduler.release("adc")
        self.scheduler.release("pressure")

        self.scheduler.replace([_collection("adc")])
        self.assertEqual(self.scheduler.scheduled, ("adc",))
        claimed, _ = self.scheduler.claim_due()
        self.assertEqual(self._names(claimed), ["adc"])

    def test_replace_forgets_in_flight_names_it_dropped(self):
        """Keep the in-flight set from leaking a collection that is gone."""
        self.scheduler.replace([_collection("adc"), _collection("pressure")])
        self.scheduler.claim_due()
        self.assertEqual(self.scheduler.in_flight, 2)

        self.scheduler.replace([_collection("adc")])
        self.assertEqual(self.scheduler.in_flight, 1)

    def test_release_of_an_unknown_name_is_harmless(self):
        """Tolerate a tick finishing for a collection a reload removed."""
        self.scheduler.release("nosuch")
        self.assertEqual(self.scheduler.in_flight, 0)


if __name__ == "__main__":
    unittest.main()
