"""The keygrabber daemon: schedule reads, collect samples, write them out."""
from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, List, Mapping, Optional, Sequence

from ..client import Client
from ..config import ConfigError, DaemonConfigLoader, with_env_overrides
from ..daemon import LibbyDaemon
from ..errors import LibbyError
from .channel import DRAIN_DEADLINE_S, WRITER_POLL_S, SinkChannel
from .collection import FAILURES_BEFORE_BACKOFF, Collection
from .config import TIMEOUT_HEADROOM, KeygrabberConfig, build_sink, parse_config
from .scheduler import Scheduler
from .sink import Sample, Sink

# Scheduler wake interval, so a stop request is noticed promptly even when the
# next tick is far off
SCHEDULER_TICK_S = 0.25

# Batches held between the readers and each sink's writer. Bounded so a
# stalled writer cannot grow memory; the retry queue inside RetryingWriter
# handles a stalled backend.
QUEUE_DEPTH_PER_WORKER = 4

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
    """Read-side tallies the daemon reports, guarded for cross-thread increments.

    The lock is only ever held around an increment, never across a read or a
    sink write, because these are served on the transport's receive thread and
    blocking it would time out every read already in flight. Write-side tallies
    are each sink's, on its :class:`SinkChannel`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.read_errors = 0
        self.skipped_ticks = 0

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
    """Polls keywords from other libby peers and writes them to every sink.

    One process serves the whole fleet: cadence lives in this daemon's config
    rather than in each hardware daemon, and the database credentials live in
    one place. Each keyword is read once per tick, whatever the number of sinks.
    """

    transport = "rabbitmq"
    discovery_enabled = False

    def __init__(self) -> None:
        super().__init__()
        self.counters = Counters()
        self.enabled = True
        self._settings: Optional[KeygrabberConfig] = None
        self._client: Optional[Client] = None
        self._channels: List[SinkChannel] = []
        self._collections: List[Collection] = []
        self._pool: Optional[ThreadPoolExecutor] = None
        self._threads: List[threading.Thread] = []
        self._halt = threading.Event()
        self._scheduler = Scheduler()
        self._source: Optional[_ConfigSource] = None

    def on_start(self) -> None:
        """Parse config, open the sinks, and start the reader and writer threads."""
        self._settings = parse_config(self._config)
        self._client = Client(self.libby)
        queue_size = max(1, self._settings.workers * QUEUE_DEPTH_PER_WORKER)
        self._channels = [
            SinkChannel(name, self.make_sink(name, sink_config),
                        policy=self._settings.retry, queue_size=queue_size,
                        logger=self.logger)
            for name, sink_config in self._settings.sinks.items()
        ]
        for channel in self._channels:
            channel.connect()
        self._collections = [Collection(config)
                             for config in self._settings.collections]
        self._pool = ThreadPoolExecutor(max_workers=self._settings.workers,
                                        thread_name_prefix="keygrabber-read")
        self._scheduler.replace(self._collections)
        self._register_control_keywords()
        self._halt.clear()
        for channel in self._channels:
            channel.thread = self._spawn(lambda c=channel: c.run(self._halt),
                                         f"keygrabber-write-{channel.name}")
        self._spawn(self._schedule_loop, "keygrabber-schedule")
        self.logger.info("collecting %d collections with %d workers into %s",
                         len(self._collections), self._settings.workers,
                         ", ".join(channel.name for channel in self._channels))

    def on_stop(self) -> None:
        """Stop the threads and close each sink once its writer has drained it.

        The draining happens on each sink's writer thread rather than here, so
        only one thread is ever inside a sink. This method just waits for them.
        """
        self._halt.set()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for thread in self._threads:
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
        self._threads = []

        channels, self._channels = self._channels, []
        for channel in channels:
            if channel.thread is not None and channel.thread.is_alive():
                # Closing while the writer is still inside a sink call would put
                # two threads in one sink; leave it to process exit instead
                self.logger.error(
                    "sink %s: writer did not finish within %.1fs; leaving it open",
                    channel.name, STOP_JOIN_TIMEOUT_S)
                continue
            channel.close()

    @classmethod
    def from_config_file(cls, path, daemon_id=None, *, env_prefix=None):
        """Build from a file, remembering where it came from for ``reload``."""
        daemon = super().from_config_file(path, daemon_id, env_prefix=env_prefix)
        daemon._source = _ConfigSource(str(path), daemon_id, env_prefix)
        return daemon

    def make_sink(self, name: str, sink_config: Mapping[str, Any]) -> Sink:
        """Build one configured sink.

        Override to supply a sink the config cannot describe, or to inject one
        in a test without installing a database client.
        """
        return build_sink(name, sink_config)

    @property
    def points_written(self) -> int:
        """Samples stored, summed over every sink."""
        return sum(channel.points_written for channel in self._channels)

    @property
    def write_errors(self) -> int:
        """Failed sink writes, summed over every sink."""
        return sum(channel.write_errors for channel in self._channels)

    @property
    def dropped_batches(self) -> int:
        """Batches dropped on a full queue, summed over every sink."""
        return sum(channel.dropped_batches for channel in self._channels)

    ### Control surface

    def _register_control_keywords(self) -> None:
        """Expose the daemon's own state as keywords.

        Every getter here is answered on the transport's receive thread, so
        none of them may block: the counters are plain reads, ``isconnected``
        reports flags the writer threads maintain rather than pinging a
        database, and writing it only requests a reconnect. The unprefixed
        sink keywords cover every sink; ``<sink>.*`` report one.
        """
        registry = self.keyword_registry
        registry.bool("enabled",
                      getter=lambda: self.enabled,
                      setter=self._set_enabled,
                      description="Collect on the configured cadences; "
                                  "write false to pause without exiting.")
        registry.bool("isconnected",
                      getter=lambda: all(c.healthy for c in self._channels),
                      setter=lambda value: self._set_connected(self._channels, value),
                      description="Last write to every sink succeeded; write true "
                                  "to request a reconnect of each.")
        registry.int("pointswritten", getter=lambda: self.points_written,
                     description="Samples stored since start, summed over sinks.")
        registry.int("readerrors", getter=lambda: self.counters.read_errors,
                     description="Keyword reads that failed.")
        registry.int("writeerrors", getter=lambda: self.write_errors,
                     description="Sink writes that failed, summed over sinks.")
        registry.int("queuedepth",
                     getter=lambda: sum(c.queue_depth for c in self._channels),
                     description="Batches waiting in retry queues, summed over sinks.")
        registry.int("skippedticks", getter=lambda: self.counters.skipped_ticks,
                     description="Ticks skipped because the previous read "
                                 "was still in flight.")
        registry.int("droppedbatches", getter=lambda: self.dropped_batches,
                     description="Batches discarded because a sink's queue was "
                                 "full, summed over sinks.")
        registry.trigger("reload", action=self._reload,
                         description="Re-read the config file and apply it.")
        registry.trigger("shutdown", action=self.request_stop,
                         description="Gracefully stop this daemon.")
        for channel in self._channels:
            self._register_sink_keywords(channel)
        for collection in self._collections:
            self._register_collection_keywords(collection)

    def _register_sink_keywords(self, channel: SinkChannel) -> None:
        """Expose one sink's health, so a failing one can be told apart."""
        name = channel.name
        registry = self.keyword_registry
        registry.bool(f"{name}.isconnected",
                      getter=lambda c=channel: c.healthy,
                      setter=lambda value, c=channel: self._set_connected([c], value),
                      description=f"Last write to {name} succeeded; write true "
                                  "to request a reconnect.")
        registry.int(f"{name}.pointswritten", getter=lambda c=channel: c.points_written,
                     description=f"Samples {name} has stored since start.")
        registry.int(f"{name}.writeerrors", getter=lambda c=channel: c.write_errors,
                     description=f"Writes to {name} that failed.")
        registry.int(f"{name}.queuedepth", getter=lambda c=channel: c.queue_depth,
                     description=f"Batches waiting in {name}'s retry queue.")
        registry.int(f"{name}.droppedbatches",
                     getter=lambda c=channel: c.dropped_batches,
                     description=f"Batches discarded because {name}'s queue was full.")

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

    @staticmethod
    def _set_connected(channels: Sequence[SinkChannel], value: bool) -> None:
        """Request a reconnect of ``channels``; refuse a write of false.

        The reconnect happens on each writer thread rather than here, because a
        dead database would otherwise hold the receive thread for the client's
        whole connect timeout. Poll ``isconnected`` for the outcome.
        """
        if not value:
            raise ValueError("write true to reconnect; there is no manual disconnect")
        for channel in channels:
            channel.request_reconnect()

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
                self._note_failure(
                    collection,
                    f"every read failed ({result.read_errors} errors), "
                    f"first: {result.first_error}")
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
        batch = tuple(samples)
        for channel in self._channels:
            channel.offer(batch)

    def _require_client(self) -> Client:
        if self._client is None:
            raise LibbyError("keygrabber has no client; it is not started")
        return self._client
