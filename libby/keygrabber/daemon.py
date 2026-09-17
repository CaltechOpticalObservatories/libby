"""The keygrabber daemon: schedule reads, collect samples, write them out."""
from __future__ import annotations

import heapq
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from ..client import Client
from ..daemon import LibbyDaemon
from ..errors import LibbyError
from ..libby import Libby
from .collection import Collection
from .config import KeygrabberConfig, build_sink, parse_config
from .sink import RetryingWriter, Sample, Sink

# How long the writer waits for a batch before checking the retry queue
WRITER_POLL_S = 0.25

# Scheduler wake interval, so a stop request is noticed promptly even when the
# next tick is far off
SCHEDULER_TICK_S = 0.25

# Batches held between the readers and the writer. Bounded so a stalled writer
# cannot grow memory; the retry queue inside RetryingWriter handles a stalled
# backend.
QUEUE_DEPTH_PER_WORKER = 4

# Time allowed for the retry queue to drain during shutdown. Must stay well
# under the systemd unit's TimeoutStopSec so a wedged sink cannot turn a stop
# into a SIGKILL.
DRAIN_DEADLINE_S = 5.0

# How long ``on_stop`` waits for a thread to finish. Outlasts the drain
# deadline, so a writer that is draining gets to finish rather than being
# abandoned mid-batch.
STOP_JOIN_TIMEOUT_S = DRAIN_DEADLINE_S + WRITER_POLL_S * 2


class Counters:  # pylint: disable=too-few-public-methods
    """Tallies the daemon reports, guarded for cross-thread increments.

    The lock is only ever held around an increment, never across a read or a
    sink write, because these are served on the transport's receive thread and
    blocking it would time out every read already in flight.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.points_written = 0
        self.read_errors = 0
        self.write_errors = 0
        self.skipped_ticks = 0
        self.dropped_batches = 0

    def add(self, **deltas: int) -> None:
        """Add to one or more counters."""
        with self._lock:
            for name, delta in deltas.items():
                setattr(self, name, getattr(self, name) + delta)


# Coordinates config, client, sink, collections and three kinds of thread;
# the attribute count is collaborators, not state
class KeygrabberDaemon(LibbyDaemon):  # pylint: disable=too-many-instance-attributes
    """Polls keywords from other libby peers and writes them to a sink.

    One process serves the whole fleet: cadence lives in this daemon's config
    rather than in each hardware daemon, and the database credential lives in
    one place.
    """

    transport = "rabbitmq"
    discovery_enabled = False

    def __init__(self) -> None:
        super().__init__()
        self.counters = Counters()
        self._settings: Optional[KeygrabberConfig] = None
        self._client: Optional[Client] = None
        self._writer: Optional[RetryingWriter] = None
        self._collections: List[Collection] = []
        self._queue: "queue.Queue[Tuple[Sample, ...]]" = queue.Queue()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._threads: List[threading.Thread] = []
        self._writer_thread: Optional[threading.Thread] = None
        self._halt = threading.Event()
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._clock: Callable[[], float] = time.monotonic

    def on_start(self, libby: Libby) -> None:
        """Parse config, open the sink, and start the reader and writer threads."""
        self._settings = parse_config(self._config)
        self._client = Client(libby)
        self._writer = RetryingWriter(self.make_sink(),
                                      policy=self._settings.retry)
        self._writer.connect()
        self._collections = [Collection(config)
                             for config in self._settings.collections]
        self._queue = queue.Queue(
            maxsize=max(1, self._settings.workers * QUEUE_DEPTH_PER_WORKER))
        self._pool = ThreadPoolExecutor(max_workers=self._settings.workers,
                                        thread_name_prefix="keygrabber-read")
        self._halt.clear()
        self._writer_thread = self._spawn(self._write_loop, "keygrabber-write")
        self._spawn(self._schedule_loop, "keygrabber-schedule")
        self.logger.info("collecting %d collections with %d workers",
                         len(self._collections), self._settings.workers)

    def on_stop(self, libby: Optional[Libby] = None) -> None:
        """Stop the threads and close the sink once the writer has drained it.

        The draining happens on the writer thread rather than here, so only one
        thread is ever inside the sink. This method just waits for it.
        """
        self._halt.set()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for thread in self._threads:
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
        self._threads = []

        writer, self._writer = self._writer, None
        if writer is None:
            return
        if self._writer_thread is not None and self._writer_thread.is_alive():
            # Closing while the writer is still inside a sink call would put
            # two threads in one sink; leave it to process exit instead
            self.logger.error(
                "writer thread did not finish within %.1fs; leaving the sink open",
                STOP_JOIN_TIMEOUT_S)
            return
        writer.close()

    def make_sink(self) -> Sink:
        """Build the configured sink.

        Override to supply a sink the config cannot describe, or to inject one
        in a test without installing a database client.
        """
        if self._settings is None:
            raise LibbyError("keygrabber config has not been parsed yet")
        return build_sink(self._settings.sink)

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    ### Scheduling

    def _schedule_loop(self) -> None:
        """Submit each collection's tick when it comes due."""
        now = self._clock()
        pending: List[Tuple[float, str]] = [
            (now, collection.name) for collection in self._collections
        ]
        heapq.heapify(pending)
        by_name = {collection.name: collection for collection in self._collections}

        while not self._halt.is_set():
            if not pending:
                self._halt.wait(SCHEDULER_TICK_S)
                continue

            due_at, name = pending[0]
            delay = due_at - self._clock()
            if delay > 0:
                self._halt.wait(min(delay, SCHEDULER_TICK_S))
                continue

            heapq.heappop(pending)
            collection = by_name[name]
            self._submit(collection)
            # Never schedule into the past: a long stall would otherwise queue
            # a burst of catch-up ticks that can only skip
            interval = collection.config.interval_s
            heapq.heappush(pending, (max(due_at + interval, self._clock()), name))

    def _submit(self, collection: Collection) -> None:
        """Run a tick unless the previous one is still going."""
        with self._in_flight_lock:
            if collection.name in self._in_flight:
                self.counters.add(skipped_ticks=1)
                self.logger.warning(
                    "collection %s skipped: previous read still in flight",
                    collection.name)
                return
            self._in_flight.add(collection.name)

        if not self._dispatch(collection):
            # Shut down between the claim and the submit, so give the claim
            # back rather than leaving the collection marked busy for good
            with self._in_flight_lock:
                self._in_flight.discard(collection.name)

    def _dispatch(self, collection: Collection) -> bool:
        """Submit a tick, returning False once the pool can no longer take one."""
        pool = self._pool
        if pool is None:
            return False
        try:
            pool.submit(self._run_tick, collection)
            return True
        except RuntimeError:
            # ThreadPoolExecutor.submit raises once shutdown() has been called,
            # which races the scheduler thread on the way out
            return False

    def _run_tick(self, collection: Collection) -> None:
        """Resolve if due, read once, and hand the samples to the writer."""
        try:
            if collection.needs_resolve():
                collection.resolve(self._require_client())
            result = collection.tick(self._require_client(),
                                     datetime.now(timezone.utc))
            if result.read_errors:
                self.counters.add(read_errors=result.read_errors)
            if result.samples:
                self._enqueue(result.samples)
        except LibbyError as exc:
            self.counters.add(read_errors=1)
            self.logger.error("collection %s read failed: %s", collection.name, exc)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(collection.name)

    def _enqueue(self, samples: Sequence[Sample]) -> None:
        try:
            self._queue.put_nowait(tuple(samples))
        except queue.Full:
            self.counters.add(dropped_batches=1)
            self.logger.error("sample queue full; dropped %d samples",
                              len(samples))

    ### Writing

    def _write_loop(self) -> None:
        """Own every sink call, so no reader thread ever touches the backend."""
        while not self._halt.is_set():
            try:
                batch = self._queue.get(timeout=WRITER_POLL_S)
            except queue.Empty:
                self._flush()
                continue
            self._write(batch)
            self._flush()
        # Halted: drain here, on the one thread allowed to touch the sink
        self._drain()

    def _write(self, batch: Tuple[Sample, ...]) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            self.counters.add(points_written=writer.write(batch))
        except LibbyError as exc:
            self.counters.add(write_errors=1)
            self.logger.error("sink write failed: %s", exc)

    def _flush(self) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            self.counters.add(points_written=writer.flush_due())
        except LibbyError as exc:
            self.counters.add(write_errors=1)
            self.logger.error("sink retry failed: %s", exc)

    def _drain(self) -> None:
        """Write whatever is still queued, under a deadline."""
        deadline = self._clock() + DRAIN_DEADLINE_S
        while self._clock() < deadline:
            try:
                self._write(self._queue.get_nowait())
                continue
            except queue.Empty:
                pass
            writer = self._writer
            if writer is None or writer.queue_depth == 0:
                return
            self._flush()
            if writer.queue_depth:
                time.sleep(WRITER_POLL_S)
        if self._writer is not None and self._writer.queue_depth:
            self.logger.error("gave up draining %d batches after %.1fs",
                              self._writer.queue_depth, DRAIN_DEADLINE_S)

    def _require_client(self) -> Client:
        if self._client is None:
            raise LibbyError("keygrabber has no client; it is not started")
        return self._client
