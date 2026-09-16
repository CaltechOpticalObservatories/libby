"""Unit tests for RetryingWriter: queueing, backoff, bounds and ordering.

The clock is injected, so nothing here waits for a real backoff window.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

from libby.keygrabber import RetryingWriter, RetryPolicy, Sample, SinkWriteError


def _sample(keyword: str = "positionvalue", value: float = 1.0) -> Sample:
    return Sample(keyword=keyword, group="hsfei", peer="adc", value=value,
                  units="mm", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))


class _FakeClock:
    """Manually advanced clock, so backoff is exercised without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class _FakeSink:
    """Sink that records what it was given and fails on demand."""

    def __init__(self) -> None:
        self.batches: List[Tuple[Sample, ...]] = []
        self.attempts = 0
        self.failing = False
        self.connects = 0
        self.closed = False

    def connect(self) -> None:
        """Count reconnections."""
        self.connects += 1

    def is_connected(self) -> bool:
        """Report the inverse of the failing flag."""
        return not self.failing

    def write(self, samples: Sequence[Sample]) -> int:
        """Record the batch, or raise while failing."""
        self.attempts += 1
        if self.failing:
            raise SinkWriteError("backend down")
        self.batches.append(tuple(samples))
        return len(samples)

    def close(self) -> None:
        """Mark the sink closed."""
        self.closed = True


class RetryingWriterTests(unittest.TestCase):
    """Behaviour of the retry queue in front of a sink."""

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.sink = _FakeSink()
        self.writer = RetryingWriter(
            self.sink,
            policy=RetryPolicy(max_batches=3, base_backoff_s=1.0, max_backoff_s=8.0),
            clock=self.clock)

    def test_healthy_write_goes_straight_through(self):
        """Pass a batch to the sink and report what it wrote."""
        self.assertEqual(self.writer.write([_sample(), _sample()]), 2)
        self.assertEqual(len(self.sink.batches), 1)
        self.assertEqual(self.writer.queue_depth, 0)

    def test_empty_batch_is_not_written(self):
        """Skip an empty batch rather than making a pointless request."""
        self.assertEqual(self.writer.write([]), 0)
        self.assertEqual(self.sink.batches, [])

    def test_failed_write_is_queued_not_lost(self):
        """Hold a failed batch for retry and report nothing written."""
        self.sink.failing = True
        self.assertEqual(self.writer.write([_sample()]), 0)
        self.assertEqual(self.writer.queue_depth, 1)

    def test_queued_batch_is_written_once_backoff_elapses(self):
        """Retry the backlog after the backoff window, not before."""
        self.sink.failing = True
        self.writer.write([_sample()])
        self.sink.failing = False

        self.assertEqual(self.writer.flush_due(), 0)  # still inside backoff
        self.assertEqual(self.writer.queue_depth, 1)

        self.clock.advance(1.0)
        self.assertEqual(self.writer.flush_due(), 1)
        self.assertEqual(self.writer.queue_depth, 0)

    def test_backoff_grows_and_is_capped(self):
        """Wait longer between retries as failures repeat, up to the ceiling."""
        self.sink.failing = True
        self.writer.write([_sample()])          # first failure arms the backoff

        waits = []
        for _ in range(5):
            attempts_before = self.sink.attempts
            waited = 0.0
            # Measure when the writer next touches the sink at all, since a
            # retry that fails is still a retry
            while self.sink.attempts == attempts_before and waited < 30.0:
                self.clock.advance(0.5)
                waited += 0.5
                self.writer.flush_due()
            waits.append(waited)
        self.assertEqual(waits[:4], [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(waits[4], 8.0)         # capped at max_backoff_s

    def test_queue_drops_oldest_when_full(self):
        """Bound the queue so a dead backend cannot grow memory without limit."""
        self.sink.failing = True
        for index in range(5):
            self.writer.write([_sample(value=float(index))])
        self.assertEqual(self.writer.queue_depth, 3)
        self.assertEqual(self.writer.dropped_batches, 2)

        self.sink.failing = False
        self.clock.advance(100.0)
        self.writer.flush_due()
        # The two oldest went, the three newest survived in order
        written = [batch[0].value for batch in self.sink.batches]
        self.assertEqual(written, [2.0, 3.0, 4.0])

    def test_write_queues_behind_an_existing_backlog(self):
        """Keep ordering by not overtaking a backlog with a fresh batch."""
        self.sink.failing = True
        self.writer.write([_sample(value=1.0)])
        self.sink.failing = False
        self.assertEqual(self.writer.write([_sample(value=2.0)]), 0)
        self.assertEqual(self.writer.queue_depth, 2)

        self.clock.advance(1.0)
        self.writer.flush_due()
        self.assertEqual([batch[0].value for batch in self.sink.batches], [1.0, 2.0])

    def test_flush_stops_at_the_first_failure(self):
        """Leave the rest of the backlog queued when a retry fails again."""
        self.sink.failing = True
        for index in range(3):
            self.writer.write([_sample(value=float(index))])
        self.clock.advance(100.0)
        self.assertEqual(self.writer.flush_due(), 0)
        self.assertEqual(self.writer.queue_depth, 3)

    def test_reconnect_clears_backoff(self):
        """Let an operator-driven reconnect retry immediately."""
        self.sink.failing = True
        self.writer.write([_sample()])
        self.sink.failing = False
        self.writer.connect()
        self.assertEqual(self.sink.connects, 1)
        self.assertEqual(self.writer.flush_due(), 1)

    def test_delegates_connection_state_and_close(self):
        """Pass health and teardown through to the wrapped sink."""
        self.assertTrue(self.writer.is_connected())
        self.sink.failing = True
        self.assertFalse(self.writer.is_connected())
        self.writer.close()
        self.assertTrue(self.sink.closed)

    def test_policy_rejects_a_queue_that_holds_nothing(self):
        """Refuse a queue bound that could never retain a batch."""
        with self.assertRaises(ValueError):
            RetryPolicy(max_batches=0)

    def test_policy_rejects_a_non_positive_backoff(self):
        """Refuse a backoff that would retry a dead backend without pause."""
        with self.assertRaises(ValueError):
            RetryPolicy(base_backoff_s=0.0)


if __name__ == "__main__":
    unittest.main()
