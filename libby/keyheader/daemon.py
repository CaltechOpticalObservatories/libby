"""The keyheader daemon: answer a named segment at a moment with FITS cards."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

from ..config import ConfigError, DaemonConfigLoader, with_env_overrides
from ..daemon import LibbyDaemon
from ..errors import LibbyError
from .acquire import Acquired, acquire, query_range
from .cards import Entry, fits_date, segment_entries
from .config import CardConfig, KeyheaderConfig, build_source, parse_config
from .source import Source, SourceError

SERVICE_KEY = "keyheader.segment"


class Counters:  # pylint: disable=too-few-public-methods
    """Tallies the daemon reports, guarded for cross-thread increments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.read_errors = 0
        self.blank_cards = 0

    def add(self, **deltas: int) -> None:
        """Add to one or more counters."""
        with self._lock:
            for name, delta in deltas.items():
                setattr(self, name, getattr(self, name) + delta)


# Coordinates config, source, pool and reload state; the attribute count is
# collaborators, not state
class KeyheaderDaemon(LibbyDaemon):  # pylint: disable=too-many-instance-attributes
    """Answers ``keyheader.segment`` requests with FITS card entries.

    A request names a configured segment and a moment. Every card in the
    segment is looked up in the source at that moment, and the reply is a list
    of ``{keyword, value, comment}`` entries the requesting daemon adds to its
    image header.
    """

    transport = "rabbitmq"
    discovery_enabled = False

    def __init__(self) -> None:
        super().__init__()
        self.counters = Counters()
        self._settings: Optional[KeyheaderConfig] = None
        self._source: Optional[Source] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._source_healthy = True
        self._config_path: Optional[str] = None
        self._daemon_id: Optional[str] = None
        self._env_prefix: Optional[str] = None

    def on_start(self) -> None:
        """Parse config, open the source, and start answering requests."""
        self.configure(parse_config(self._config))
        self.add_service(SERVICE_KEY, self._serve)
        self._register_control_keywords()
        self.logger.info("serving %d segments: %s", len(self._require().segments),
                         ", ".join(s.name for s in self._require().segments))

    def on_stop(self) -> None:
        """Stop the query pool and release the source."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)
        source, self._source = self._source, None
        if source is not None:
            source.close()

    def configure(self, settings: KeyheaderConfig,
                  source: Optional[Source] = None) -> None:
        """Apply parsed settings and open the source.

        Called by ``on_start``; a test calls it directly with a fake source to
        exercise :meth:`segment` without a transport or a database.
        """
        self._settings = settings
        self._source = source if source is not None else self.make_source()
        self._source.connect()
        self._pool = ThreadPoolExecutor(max_workers=settings.workers,
                                        thread_name_prefix="keyheader-read")

    @classmethod
    def from_config_file(cls, path, daemon_id=None, *, env_prefix=None):
        """Build from a file, remembering where it came from for ``reload``."""
        daemon = super().from_config_file(path, daemon_id, env_prefix=env_prefix)
        daemon._config_path = str(path)
        daemon._daemon_id = daemon_id
        daemon._env_prefix = env_prefix
        return daemon

    def make_source(self) -> Source:
        """Build the configured source.

        Override to supply a source the config cannot describe, or to inject
        one in a test without installing a database client.
        """
        return build_source(self._require().source)

    ### Requests

    def segment(self, name: str, moment: datetime) -> List[Entry]:
        """Return the entries for one segment at one moment.

        Each card is read on the pool, so a segment costs about one query's
        latency rather than one per card. A card whose read fails is written
        blank rather than failing the request: one missing value should not
        cost the exposure its whole header.
        """
        settings = self._require()
        segment = settings.segment(name)
        pool = self._pool
        if pool is None:
            raise LibbyError("keyheader is not started")
        results = list(pool.map(lambda card: self._acquire(card, moment),
                                segment.cards))
        self.counters.add(
            requests=1,
            blank_cards=sum(1 for result in results if result.reason))
        return segment_entries(segment, moment, results)

    def _serve(self, payload: Mapping[str, Any]) -> dict:
        """Handle a ``keyheader.segment`` request."""
        name = payload.get("segment")
        if not isinstance(name, str) or not name:
            raise ValueError("request needs a 'segment' name")
        moment = parse_time(payload.get("time"))
        entries = self.segment(name, moment)
        return {"ok": True, "segment": name, "time": fits_date(moment),
                "cards": entries}

    def _acquire(self, card: CardConfig, moment: datetime) -> Acquired:
        source = self._source
        if source is None:
            return Acquired.missing("not started")
        start, stop = query_range(moment, card.mode, card.window_s)
        try:
            samples = source.read(card.keyword, start, stop,
                                  group=card.group, peer=card.peer)
        except SourceError as exc:
            self.counters.add(read_errors=1)
            self._note_source(False, str(exc))
            return Acquired.missing("read failed")
        self._note_source(True)
        return acquire(samples, moment, card.mode)

    def _note_source(self, healthy: bool, reason: str = "") -> None:
        """Track source health, logging only when it changes."""
        if healthy == self._source_healthy:
            return
        self._source_healthy = healthy
        if healthy:
            self.logger.info("source reads are succeeding again")
        else:
            self.logger.error("source read failed: %s", reason)

    ### Control surface

    def _register_control_keywords(self) -> None:
        registry = self.keyword_registry
        registry.bool("isconnected", getter=lambda: self._source_healthy,
                      description="Last source read succeeded.")
        registry.int("requests", getter=lambda: self.counters.requests,
                     description="Segment requests answered since start.")
        registry.int("readerrors", getter=lambda: self.counters.read_errors,
                     description="Source reads that failed.")
        registry.int("blankcards", getter=lambda: self.counters.blank_cards,
                     description="Cards answered with an undefined value.")
        registry.string("segments", getter=self._segment_names,
                        description="Configured segment names, comma separated.")
        registry.trigger("reload", action=self._reload,
                         description="Re-read the config file and apply its "
                                     "segments.")
        registry.trigger("shutdown", action=self.request_stop,
                         description="Gracefully stop this daemon.")

    def _segment_names(self) -> str:
        settings = self._settings
        return ",".join(s.name for s in settings.segments) if settings else ""

    def _reload(self) -> None:
        """Re-read the config file and swap in its segments.

        Segments are not keywords, so adding or removing them needs no restart.
        A change to ``source`` or ``workers`` does, and is refused rather than
        half-applied. A request in flight keeps the settings it started with.
        """
        if self._config_path is None:
            raise LibbyError("this keyheader was not built from a config file")
        config = DaemonConfigLoader(self._config_path).get_daemon_config(
            self._daemon_id)
        if self._env_prefix:
            config = with_env_overrides(config, prefix=self._env_prefix)
        settings = parse_config(config)
        running = self._require()
        if (dict(settings.source) != dict(running.source)
                or settings.workers != running.workers):
            raise ConfigError("reload cannot change source or workers; "
                              "restart instead")
        self._settings = settings
        self.logger.info("reloaded %d segments from %s",
                         len(settings.segments), self._config_path)

    def _require(self) -> KeyheaderConfig:
        if self._settings is None:
            raise LibbyError("keyheader config has not been parsed yet")
        return self._settings


def parse_time(raw: Any) -> datetime:
    """Parse a request's ISO 8601 time; a time with no offset is taken as UTC."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("request needs a 'time' in ISO 8601, e.g. "
                         "'2026-11-11T08:15:02.123Z'")
    text = raw.strip()
    # fromisoformat accepts a trailing Z only from Python 3.11
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"time {raw!r} is not ISO 8601") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment
