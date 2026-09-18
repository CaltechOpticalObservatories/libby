"""The keygrabber daemon: schedule reads, collect samples, write them out."""
from __future__ import annotations

import dataclasses
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from ..client import Client
from ..config import ConfigError, DaemonConfigLoader, with_env_overrides
from ..daemon import LibbyDaemon
from ..errors import LibbyError
from ..libby import Libby
from .collection import FAILURES_BEFORE_BACKOFF, Collection
from .config import TIMEOUT_HEADROOM, KeygrabberConfig, build_sink, parse_config
from .scheduler import Scheduler
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


@dataclasses.dataclass(frozen=True)
class _ConfigSource:
    """Where a daemon's config came from, so ``reload`` can re-read it."""

    path: str
    daemon_id: Optional[str] = None
    env_prefix: Optional[str] = None


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

    def set(self, **values: int) -> None:
        """Set one or more counters to an absolute value."""
        with self._lock:
            for name, value in values.items():
                setattr(self, name, value)


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
        self.enabled = True
        self._settings: Optional[KeygrabberConfig] = None
        self._client: Optional[Client] = None
        self._writer: Optional[RetryingWriter] = None
        self._collections: List[Collection] = []
        self._queue: "queue.Queue[Tuple[Sample, ...]]" = queue.Queue()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._threads: List[threading.Thread] = []
        self._writer_thread: Optional[threading.Thread] = None
        self._halt = threading.Event()
        self._scheduler = Scheduler()
        self._source: Optional[_ConfigSource] = None
        self._sink_healthy = True
        self._reconnect_wanted = False
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
        self._scheduler.replace(self._collections)
        self._register_control_keywords()
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

    @classmethod
    def from_config_file(cls, path, daemon_id=None, *, env_prefix=None):
        """Build from a file, remembering where it came from for ``reload``."""
        daemon = super().from_config_file(path, daemon_id, env_prefix=env_prefix)
        daemon._source = _ConfigSource(str(path), daemon_id, env_prefix)
        return daemon

    def make_sink(self) -> Sink:
        """Build the configured sink.

        Override to supply a sink the config cannot describe, or to inject one
        in a test without installing a database client.
        """
        if self._settings is None:
            raise LibbyError("keygrabber config has not been parsed yet")
        return build_sink(self._settings.sink)

    ### Control surface

    def _register_control_keywords(self) -> None:
        """Expose the daemon's own state as keywords.

        Every getter here is answered on the transport's receive thread, so
        none of them may block: the counters are plain reads, ``isconnected``
        reports a flag the writer thread maintains rather than pinging the
        database, and writing it only requests a reconnect.
        """
        registry = self.keyword_registry
        registry.bool("enabled",
                      getter=lambda: self.enabled,
                      setter=self._set_enabled,
                      description="Collect on the configured cadences; "
                                  "write false to pause without exiting.")
        registry.bool("isconnected",
                      getter=lambda: self._sink_healthy,
                      setter=self._set_connected,
                      description="Last sink write succeeded; write true to "
                                  "request a reconnect.")
        registry.int("pointswritten", getter=lambda: self.counters.points_written,
                     description="Samples the sink has stored since start.")
        registry.int("readerrors", getter=lambda: self.counters.read_errors,
                     description="Keyword reads that failed.")
        registry.int("writeerrors", getter=lambda: self.counters.write_errors,
                     description="Sink writes that failed.")
        registry.int("queuedepth", getter=self._queue_depth,
                     description="Batches waiting in the retry queue.")
        registry.int("skippedticks", getter=lambda: self.counters.skipped_ticks,
                     description="Ticks skipped because the previous read "
                                 "was still in flight.")
        registry.int("droppedbatches", getter=lambda: self.counters.dropped_batches,
                     description="Batches discarded because a queue was full.")
        registry.trigger("reload", action=self._reload,
                         description="Re-read the config file and apply it.")
        registry.trigger("shutdown", action=self.request_stop,
                         description="Gracefully stop this daemon.")
        for collection in self._collections:
            self._register_collection_keywords(collection)

    def _register_collection_keywords(self, collection: Collection) -> None:
        """Expose one collection's cadence and health."""
        name = collection.name
        registry = self.keyword_registry
        registry.bool(f"{name}.enabled",
                      getter=lambda c=collection: c.enabled,
                      setter=lambda value, c=collection: setattr(c, "enabled", value),
                      description=f"Collect {name}; write false to pause it.")
        registry.float(f"{name}.interval",
                       getter=lambda c=collection: c.config.interval_s,
                       setter=lambda value, n=name: self._set_interval(n, value),
                       units="seconds",
                       description=f"Cadence for {name}.")
        registry.string(f"{name}.lastsample",
                        getter=lambda c=collection: (
                            c.last_sample.isoformat() if c.last_sample else None),
                        nullable=True,
                        description=f"UTC time of the last successful {name} tick.")
        registry.float(f"{name}.lag",
                       getter=lambda c=collection: c.lag_s,
                       units="seconds",
                       description=f"Seconds the last {name} tick ran past due.")

    def _set_enabled(self, value: bool) -> None:
        """Pause or resume all collection."""
        self.enabled = value
        self.logger.info("collection %s", "enabled" if value else "paused")

    def _set_connected(self, value: bool) -> None:
        """Request a reconnect; refuse a write of false.

        The reconnect happens on the writer thread rather than here, because a
        dead database would otherwise hold the receive thread for the client's
        whole connect timeout. Poll ``isconnected`` for the outcome.
        """
        if not value:
            raise ValueError("write true to reconnect; there is no manual disconnect")
        self._reconnect_wanted = True

    def _set_interval(self, name: str, value: float) -> None:
        """Change one collection's cadence, keeping the timeout headroom rule."""
        collection = self._collection(name)
        minimum = TIMEOUT_HEADROOM * collection.config.timeout_s
        if value <= minimum:
            raise ValueError(
                f"interval must exceed {minimum}s, which is "
                f"{TIMEOUT_HEADROOM} x this collection's timeout")
        collection.config = dataclasses.replace(collection.config,
                                                interval_s=value)
        self._scheduler.update(collection)

    def _collection(self, name: str) -> Collection:
        for collection in self._collections:
            if collection.name == name:
                return collection
        raise ValueError(f"unknown collection {name!r}")

    def _queue_depth(self) -> int:
        writer = self._writer
        return writer.queue_depth if writer is not None else 0

    def _reload(self) -> None:
        """Re-read the config file and apply it to the running collections.

        Parsed here, on the receive thread, so a bad file is reported straight
        back to the caller and the running set is left untouched. Adding or
        removing a collection is refused rather than half-applied: libby has no
        way to withdraw a keyword, so a new collection's control keywords could
        not appear without a restart.
        """
        if self._source is None:
            raise LibbyError("this keygrabber was not built from a config file")

        config = DaemonConfigLoader(self._source.path).get_daemon_config(
            self._source.daemon_id)
        if self._source.env_prefix:
            config = with_env_overrides(config, prefix=self._source.env_prefix)
        settings = parse_config(config)

        running = {collection.name for collection in self._collections}
        reloaded = {collection.name for collection in settings.collections}
        if running != reloaded:
            raise ConfigError(
                "reload cannot add or remove collections "
                f"({sorted(running)} -> {sorted(reloaded)}); restart instead"
            )

        by_name = {collection.name: collection for collection in self._collections}
        for config_entry in settings.collections:
            collection = by_name[config_entry.name]
            collection.config = config_entry
            # The keyword selection may have changed, so do not wait out the
            # old refresh window before picking it up
            collection.invalidate()
        self._settings = settings
        self._scheduler.replace(self._collections)
        self.logger.info("reloaded %d collections from %s",
                         len(self._collections), self._source.path)

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    ### Scheduling

    def _schedule_loop(self) -> None:
        """Submit each collection's tick when the scheduler says it is due."""
        while not self._halt.is_set():
            if self.enabled:
                self._submit_due()
            delay = self._scheduler.next_delay()
            self._halt.wait(SCHEDULER_TICK_S if delay is None
                            else min(delay, SCHEDULER_TICK_S))

    def _submit_due(self) -> None:
        """Hand every due collection to the pool, counting the skips."""
        claimed, skipped = self._scheduler.claim_due()
        if skipped:
            self.counters.add(skipped_ticks=skipped)
            self.logger.warning("%d tick(s) skipped: previous read still in flight",
                                skipped)
        for collection in claimed:
            if not collection.enabled or not self._dispatch(collection):
                # Paused, or shut down between the claim and the submit: give
                # the claim back rather than leaving it marked busy for good
                self._scheduler.release(collection.name)

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
                collection.last_sample = datetime.now(timezone.utc)
                self._note_success(collection)
            elif collection.keyword_count:
                self._note_failure(collection, "every read failed")
        except LibbyError as exc:
            self.counters.add(read_errors=1)
            self._note_failure(collection, str(exc))
        finally:
            self._scheduler.release(collection.name)

    def _note_failure(self, collection: Collection, reason: str) -> None:
        """Count a failed tick, and log only while that is still news.

        A peer that stays down would otherwise produce an error every interval
        for as long as it is down, which buries everything else and keeps
        rewriting ``lasterror``.
        """
        collection.note_failure()
        failures = collection.consecutive_failures
        if failures < FAILURES_BEFORE_BACKOFF:
            self.logger.error("collection %s failed: %s", collection.name, reason)
        elif failures == FAILURES_BEFORE_BACKOFF:
            self.logger.error(
                "collection %s has failed %d times, backing off to %.0fs: %s",
                collection.name, failures, collection.backoff_interval_s(), reason)

    def _note_success(self, collection: Collection) -> None:
        """Clear a collection's backoff, saying so if it had been failing."""
        if collection.consecutive_failures >= FAILURES_BEFORE_BACKOFF:
            self.logger.info("collection %s is answering again", collection.name)
        collection.note_success()

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
            self._reconnect_if_requested()
            try:
                batch = self._queue.get(timeout=WRITER_POLL_S)
            except queue.Empty:
                self._flush()
                continue
            self._write(batch)
            self._flush()
        # Halted: drain here, on the one thread allowed to touch the sink
        self._drain()

    def _reconnect_if_requested(self) -> None:
        """Honour an isconnected write, on this thread rather than the caller's."""
        if not self._reconnect_wanted:
            return
        self._reconnect_wanted = False
        writer = self._writer
        if writer is None:
            return
        try:
            writer.connect()
            self._sink_healthy = True
            self.logger.info("reconnected to the sink on request")
        except LibbyError as exc:
            self._sink_healthy = False
            self.logger.error("sink reconnect failed: %s", exc)

    def _write(self, batch: Tuple[Sample, ...]) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            self.counters.add(points_written=writer.write(batch))
        except LibbyError as exc:
            # A sink raising something other than SinkWriteError is a bug in
            # that sink; the retry writer turns the expected failure into a
            # queued batch instead
            self.logger.error("sink write raised: %s", exc)
        self._track_sink_health(writer)

    def _flush(self) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            self.counters.add(points_written=writer.flush_due())
        except LibbyError as exc:
            self.logger.error("sink retry raised: %s", exc)
        self._track_sink_health(writer)

    def _track_sink_health(self, writer: RetryingWriter) -> None:
        """Mirror the writer's view, and log only when it changes.

        ``RetryingWriter.write`` queues a failed batch and returns 0 rather
        than raising, which is what makes retrying possible. It also means a
        failure cannot be noticed from an exception here, so health and the
        error count are read back from the writer instead.
        """
        self.counters.set(write_errors=writer.failed_attempts)
        healthy = writer.healthy
        if healthy == self._sink_healthy:
            return
        self._sink_healthy = healthy
        if healthy:
            self.logger.info("sink writes are succeeding again")
        else:
            self.logger.error("sink writes are failing, %d batch(es) queued",
                              writer.queue_depth)

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
