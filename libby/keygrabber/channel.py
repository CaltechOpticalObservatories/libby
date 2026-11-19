"""One sink's write path: its retry writer, queue, thread and health."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional, Tuple

from ..errors import LibbyError
from .sink import RetryingWriter, RetryPolicy, Sample, Sink

# How long the writer waits for a batch before checking the retry queue
WRITER_POLL_S = 0.25

# Time allowed for the retry queue to drain during shutdown. Must stay well
# under the systemd unit's TimeoutStopSec so a wedged sink cannot turn a stop
# into a SIGKILL.
DRAIN_DEADLINE_S = 5.0


# The attributes are the sink's writer, queue, thread and the health it
# reports, each one value rather than hidden state
class SinkChannel:  # pylint: disable=too-many-instance-attributes
    """Writes batches to one sink, independently of every other sink.

    Each sink has its own bounded queue and writer thread, so one that stalls
    or fails fills and drops only its own queue, and never holds up the
    readers or another sink. Only this channel's thread calls into its sink.
    """

    def __init__(
        self,
        name: str,
        sink: Sink,
        *,
        policy: RetryPolicy,
        queue_size: int,
        logger: logging.Logger,
    ) -> None:
        self.name = name
        self.healthy = True
        self.points_written = 0
        self.dropped_batches = 0
        self.thread: Optional[threading.Thread] = None
        self._writer = RetryingWriter(sink, policy=policy)
        self._queue: "queue.Queue[Tuple[Sample, ...]]" = queue.Queue(maxsize=queue_size)
        self._logger = logger
        self._reconnect_wanted = False
        # Readers on several threads count drops; only ever held for the increment
        self._lock = threading.Lock()

    @property
    def write_errors(self) -> int:
        """Attempts on the sink that failed, including retries."""
        return self._writer.failed_attempts

    @property
    def queue_depth(self) -> int:
        """Batches waiting in the retry queue."""
        return self._writer.queue_depth

    def connect(self) -> None:
        """Open the sink."""
        self._writer.connect()

    def offer(self, batch: Tuple[Sample, ...]) -> None:
        """Queue a batch for this sink, dropping it if the queue is full."""
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            with self._lock:
                self.dropped_batches += 1
            self._logger.error("sink %s: sample queue full; dropped %d samples",
                               self.name, len(batch))

    def request_reconnect(self) -> None:
        """Ask the writer thread to reconnect, rather than blocking the caller."""
        self._reconnect_wanted = True

    def run(self, halt: threading.Event) -> None:
        """Write batches until ``halt`` is set, then drain under a deadline."""
        while not halt.is_set():
            self._reconnect_if_requested()
            try:
                batch = self._queue.get(timeout=WRITER_POLL_S)
            except queue.Empty:
                self._flush()
                continue
            self._write(batch)
            self._flush()
        self._drain()

    def close(self) -> None:
        """Release the sink."""
        self._writer.close()

    def _reconnect_if_requested(self) -> None:
        if not self._reconnect_wanted:
            return
        self._reconnect_wanted = False
        try:
            self._writer.connect()
            self.healthy = True
            self._logger.info("sink %s: reconnected on request", self.name)
        except LibbyError as exc:
            self.healthy = False
            self._logger.error("sink %s: reconnect failed: %s", self.name, exc)

    def _write(self, batch: Tuple[Sample, ...]) -> None:
        try:
            self.points_written += self._writer.write(batch)
        except LibbyError as exc:
            # A sink raising something other than SinkWriteError is a bug in
            # that sink; the retry writer turns the expected failure into a
            # queued batch instead
            self._logger.error("sink %s: write raised: %s", self.name, exc)
        self._track_health()

    def _flush(self) -> None:
        try:
            self.points_written += self._writer.flush_due()
        except LibbyError as exc:
            self._logger.error("sink %s: retry raised: %s", self.name, exc)
        self._track_health()

    def _track_health(self) -> None:
        """Mirror the writer's view, and log only when it changes.

        ``RetryingWriter.write`` queues a failed batch and returns 0 rather
        than raising, which is what makes retrying possible, so health is read
        back from the writer rather than from an exception.
        """
        healthy = self._writer.healthy
        if healthy == self.healthy:
            return
        self.healthy = healthy
        if healthy:
            self._logger.info("sink %s: writes are succeeding again", self.name)
        else:
            self._logger.error("sink %s: writes are failing, %d batch(es) queued",
                               self.name, self._writer.queue_depth)

    def _drain(self) -> None:
        deadline = time.monotonic() + DRAIN_DEADLINE_S
        while time.monotonic() < deadline:
            try:
                self._write(self._queue.get_nowait())
                continue
            except queue.Empty:
                pass
            if self._writer.queue_depth == 0:
                return
            self._flush()
            if self._writer.queue_depth:
                time.sleep(WRITER_POLL_S)
        if self._writer.queue_depth:
            self._logger.error("sink %s: gave up draining %d batches after %.1fs",
                               self.name, self._writer.queue_depth, DRAIN_DEADLINE_S)
