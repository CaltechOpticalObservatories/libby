from __future__ import annotations

import collections.abc as cabc
import json
import logging
import signal
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Type, TypeVar

from .config import ConfigError, DaemonConfigLoader, with_env_overrides
from .keyword import Keyword
from .libby import Libby


Payload = Dict[str, Any]
RPCHandler = Callable[[Payload], Any]
EvtHandler = Callable[[Payload], None]
DaemonT = TypeVar("DaemonT", bound="LibbyDaemon")


class _LastErrorHandler(logging.Handler):
    """Captures a daemon's most recent ERROR+ log record.

    A daemon that only logs a failure locally (self.logger.error(...)) leaves
    that failure invisible to anyone not watching its console/log file - the
    CLI/Client see nothing (github.com/CaltechOpticalObservatories/hispec/
    issues/173). This feeds the ``lasterror`` keyword every LibbyDaemon
    exposes, so it's retrievable over RPC regardless of transport.
    """

    def __init__(self, daemon: "LibbyDaemon") -> None:
        super().__init__(level=logging.ERROR)
        self._daemon = daemon

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._daemon._last_error = self.format(record)
        except Exception:
            pass


class LibbyDaemon:
    """Base class for configurable Libby daemons.

    The class owns the following:

    * Libby transport construction
    * service and topic registration
    * typed keyword registration
    * YAML/JSON configuration loading
    * subsystem configuration selection
    * logging and lifecycle management

    Instrument-specific subclasses should normally only implement ``on_start``
    and ``on_stop``, plus any hardware-facing methods.
    """

    CONFIG_ATTRIBUTES = frozenset(
        {
            "peer_id",
            "bind",
            "address_book",
            "discovery_enabled",
            "discovery_interval_s",
            "transport",
            "rabbitmq_url",
            "group_id",
            "fail_fast_on_start",
        }
    )

    peer_id: Optional[str] = None
    bind: Optional[str] = None
    address_book: Optional[Dict[str, str]] = None
    discovery_enabled: bool = True
    discovery_interval_s: float = 5.0

    # Keep Libby's existing default. Instrument configurations can select
    # their preferred transport explicitly without changing behavior for existing ZMQ users.
    transport: str = "zmq"
    rabbitmq_url: Optional[str] = None
    group_id: Optional[str] = None

    # A daemon that fails to initialize should not continue advertising a
    # partially initialized service.
    fail_fast_on_start: bool = True

    services: Dict[str, RPCHandler] = {}
    topics: Dict[str, EvtHandler] = {}

    def __init__(self) -> None:
        # Copy subclass declarations so instances never mutate class-level maps.
        self.services = dict(getattr(type(self), "services", {}))
        self.topics = dict(getattr(type(self), "topics", {}))

        self._config: Dict[str, Any] = {}
        self._pending_keywords: List[Keyword] = []
        self._stop_event = threading.Event()
        self._started = False
        self.libby: Optional[Libby] = None
        self._last_error: Optional[str] = None
        self._start_time: Optional[float] = None
        self.logger = logging.getLogger(type(self).__name__)
        self._ensure_last_error_handler()

    @classmethod
    def config_attributes(cls) -> frozenset[str]:
        """Return config keys mapped directly onto daemon attributes.

        Subclasses may extend the set::

            CONFIG_ATTRIBUTES = (
                LibbyDaemon.CONFIG_ATTRIBUTES | {"device_name"}
            )
        """

        return cls.CONFIG_ATTRIBUTES

    @classmethod
    def from_config(
        cls: Type[DaemonT],
        config: Mapping[str, Any],
    ) -> DaemonT:
        """Build a daemon from a configuration mapping."""

        if not isinstance(config, Mapping):
            raise TypeError("daemon configuration must be a mapping")

        instance = cls()
        instance._config = dict(config)

        for attribute in instance.config_attributes():
            if attribute in config:
                setattr(instance, attribute, config[attribute])

        configured_services = config.get("services")
        if isinstance(configured_services, Mapping):
            instance.services.update(configured_services)

        configured_topics = config.get("topics")
        if isinstance(configured_topics, Mapping):
            instance.topics.update(configured_topics)

        instance._setup_logging()
        return instance

    @classmethod
    def from_config_file(
        cls: Type[DaemonT],
        path: str,
        daemon_id: Optional[str] = None,
        *,
        env_prefix: Optional[str] = None,
    ) -> DaemonT:
        """Build a daemon from a single-daemon or subsystem config file.

        For a subsystem config containing one daemon, the daemon is selected
        automatically. For multiple daemons, ``daemon_id`` is required.
        """

        loader = DaemonConfigLoader(path)

        if loader.is_subsystem and daemon_id is None:
            if len(loader.daemon_ids) == 1:
                daemon_id = loader.daemon_ids[0]
            else:
                raise ConfigError(
                    "Subsystem config contains multiple daemons: "
                    f"{loader.daemon_ids}. Specify daemon_id."
                )

        config = loader.get_daemon_config(daemon_id)
        if env_prefix:
            config = with_env_overrides(config, prefix=env_prefix)

        return cls.from_config(config)

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a configuration value using optional dot notation."""

        value: Any = self._config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value

    def _setup_logging(self) -> None:
        """Configure a daemon-local logger without resetting global logging."""

        self.logger = logging.getLogger(
            self.peer_id or type(self).__name__
        )
        self._ensure_last_error_handler()

        raw = self._config.get("logging")
        if not isinstance(raw, Mapping):
            return

        level_name = str(raw.get("level", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)
        self.logger.setLevel(level)

        if "propagate" in raw:
            self.logger.propagate = bool(raw["propagate"])

        # Only install a daemon-owned handler when explicitly requested by a
        # logging section. This avoids logging.basicConfig() changing the host
        # application's global logging policy
        if any(getattr(handler, "_libby_daemon_handler", False)
               for handler in self.logger.handlers):
            return

        log_file = raw.get("file")
        handler: logging.Handler
        if log_file:
            handler = logging.FileHandler(str(log_file))
        else:
            handler = logging.StreamHandler()

        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                str(
                    raw.get(
                        "format",
                        "%(asctime)s - %(name)s - "
                        "%(levelname)s - %(message)s",
                    )
                )
            )
        )
        setattr(handler, "_libby_daemon_handler", True)
        self.logger.addHandler(handler)

    def _ensure_last_error_handler(self) -> None:
        """Attach the lasterror-tracking handler to self.logger (idempotent).

        Called from both __init__ (covers daemons built directly, e.g. in
        tests or examples) and _setup_logging (which may point self.logger
        at a different, peer_id-named Logger once config is loaded).
        """
        if any(getattr(handler, "_libby_lasterror_handler", False)
               for handler in self.logger.handlers):
            return
        handler = _LastErrorHandler(self)
        setattr(handler, "_libby_lasterror_handler", True)
        self.logger.addHandler(handler)

    ### Optional hooks

    def on_start(self, libby: Libby) -> None:
        """Initialize hardware and register keywords."""

    def on_stop(self, libby: Optional[Libby] = None) -> None:
        """Release hardware resources."""

    def on_hello(self, libby: Libby) -> None:
        """Run after the initial discovery hello."""

    def on_event(self, topic: str, msg: Any) -> None:
        self.logger.info("%s: %s", topic, msg.env.payload)


    ### Config accessors

    def config_peer_id(self) -> str:
        return self.peer_id or self._must("peer_id")

    def config_bind(self) -> str:
        return self.bind or self._must("bind")

    def config_address_book(self) -> Dict[str, str]:
        return dict(self.address_book or {})

    def config_rabbitmq_url(self) -> str:
        return self.rabbitmq_url or "amqp://localhost"

    def config_group_id(self) -> Optional[str]:
        return self.group_id

    def config_discovery_enabled(self) -> bool:
        return bool(self.discovery_enabled)

    def config_discovery_interval_s(self) -> float:
        return float(self.discovery_interval_s)

    def config_rpc_keys(self) -> List[str]:
        return list(self.services)

    def config_subscriptions(self) -> List[str]:
        return list(self.topics)

    def add_service(self, key: str, fn: RPCHandler) -> None:
        self.services[key] = fn
        if self.libby is not None:
            self._register_services({key: fn})

    def add_services(self, mapping: Mapping[str, RPCHandler]) -> None:
        copied = dict(mapping)
        self.services.update(copied)
        if self.libby is not None:
            self._register_services(copied)

    def add_topic(self, topic: str, fn: EvtHandler) -> None:
        self.topics[topic] = fn
        if self.libby is not None:
            self._register_topics({topic: fn})

    def add_topics(self, mapping: Mapping[str, EvtHandler]) -> None:
        copied = dict(mapping)
        self.topics.update(copied)
        if self.libby is not None:
            self._register_topics(copied)

    def register_keyword(self, keyword: Keyword) -> None:
        """Register now, or defer registration until the daemon starts."""

        if self.libby is None:
            self._pending_keywords.append(keyword)
            return
        self.libby.register_keyword(keyword)

    def register_keywords(self, keywords: Iterable[Keyword]) -> None:
        """Register many keywords now, or defer until startup."""

        copied = list(keywords)
        if self.libby is None:
            self._pending_keywords.extend(copied)
            return
        self.libby.register_keywords(copied)

    @property
    def keyword_registry(self):
        """Return Libby's typed keyword builder.

        This property is intended for use in ``on_start``, after the Libby
        instance has been constructed.
        """

        if self.libby is None:
            raise RuntimeError(
                "keyword_registry is available after daemon startup; "
                "use register_keyword() to queue a keyword earlier"
            )
        return self.libby.keyword_registry

    ### Libby construction

    def build_libby(self) -> Libby:
        """Build a Libby instance for the configured transport."""

        transport = str(self.transport).strip().lower()

        if transport == "rabbitmq":
            return Libby.rabbitmq(
                self_id=self.config_peer_id(),
                rabbitmq_url=self.config_rabbitmq_url(),
                keys=[],
                callback=None,
                group_id=self.config_group_id(),
            )

        if transport == "zmq":
            return Libby.zmq(
                self_id=self.config_peer_id(),
                bind=self.config_bind(),
                address_book=self.config_address_book(),
                keys=[],
                callback=None,
                discover=self.config_discovery_enabled(),
                discover_interval_s=self.config_discovery_interval_s(),
                hello_on_start=True,
                group_id=self.config_group_id(),
            )

        raise ValueError(
            f"unsupported Libby transport {self.transport!r}; "
            "expected 'zmq' or 'rabbitmq'"
        )

    def start(self) -> None:
        """Start Libby and initialize the daemon without blocking."""

        if self._started:
            return

        self._stop_event.clear()
        self._start_time = time.monotonic()
        try:
            self.libby = self.build_libby()
            self._register_services(self.services)
            self._register_topics(self.topics)
            self.keyword_registry.string(
                "lasterror",
                getter=lambda: self._last_error,
                setter=self._clear_last_error,
                nullable=True,
                description=(
                    "Most recent ERROR-level log message from this daemon; "
                    "write null to clear."
                ),
            )
            self.keyword_registry.int(
                "uptime",
                getter=self._uptime_s,
                units="seconds",
                description="Seconds since this daemon started.",
            )

            if self.config_discovery_enabled():
                try:
                    self.libby.hello()
                    self.on_hello(self.libby)
                except Exception:
                    self.logger.exception("discovery hello failed")

            try:
                self.on_start(self.libby)
            except Exception:
                self.logger.exception("daemon initialization failed")
                if self.fail_fast_on_start:
                    self._close_libby()
                    self._started = False
                    raise

            self._flush_keywords()
            self._started = True
            self.logger.info(
                "started peer=%s transport=%s",
                self.config_peer_id(),
                self.transport,
            )
        except Exception:
            self._started = False
            raise

    def request_stop(self) -> None:
        """Request termination of a blocking ``serve`` call."""

        self._stop_event.set()

    def stop(self) -> None:
        """Stop the daemon; safe to call more than once."""

        if self.libby is None and not self._started:
            return

        try:
            self.on_stop(self.libby)
        except Exception:
            self.logger.exception("daemon shutdown hook failed")
        finally:
            self._close_libby()
            self._started = False
            self._stop_event.set()
            self.logger.info("stopped")

    def serve(self) -> None:
        """Start the daemon and block until a signal or stop request."""

        self._install_signal_handlers()
        self.start()

        try:
            while not self._stop_event.wait(0.5):
                pass
        finally:
            self.stop()

    ### Internals

    def _install_signal_handlers(self) -> None:
        # signal.signal() is only legal in Python's main thread. Skipping it
        # makes start/serve easier to exercise in test harnesses.
        if threading.current_thread() is not threading.main_thread():
            self.logger.debug("signal handlers skipped outside main thread")
            return

        def handle_signal(_signum: int, _frame: Any) -> None:
            self.request_stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    def _must(self, name: str) -> Any:
        raise ValueError(
            f"set {name!r} in the class or configuration, "
            f"or override config_{name}()"
        )

    def _clear_last_error(self, value: Any) -> None:
        """Setter for the lasterror keyword: only accepts null, to clear it."""
        if value is not None:
            raise ValueError("lasterror is read-only except to clear it (write null)")
        self._last_error = None

    def _uptime_s(self) -> int:
        """Getter for the uptime keyword: whole seconds since start()."""
        if self._start_time is None:
            return 0
        return int(time.monotonic() - self._start_time)

    def _service_adapter(self, fn: RPCHandler):
        def adapter(user_payload: dict, _ctx: dict) -> dict:
            try:
                return self.payload(fn(user_payload))
            except Exception as exc:
                self.logger.exception("service handler failed")
                return {"ok": False, "error": str(exc)}

        return adapter

    def _register_services(
        self,
        mapping: Mapping[str, RPCHandler],
    ) -> None:
        if self.libby is None:
            return

        for key, fn in mapping.items():
            self.libby.serve_keys([key], self._service_adapter(fn))

    def _register_topics(
        self,
        mapping: Mapping[str, EvtHandler],
    ) -> None:
        if self.libby is None or not mapping:
            return

        for topic, fn in mapping.items():
            self.libby.listen(
                topic,
                lambda msg, _handler=fn: _handler(msg.env.payload),
            )
        self.libby.subscribe(*mapping.keys())

    def _flush_keywords(self) -> None:
        if self.libby is None:
            return

        keywords = self._pending_keywords
        self._pending_keywords = []
        keywords.extend(self.libby.keyword_registry.drain())

        if keywords:
            self.libby.register_keywords(keywords)

    def _close_libby(self) -> None:
        libby, self.libby = self.libby, None
        if libby is not None:
            try:
                libby.stop()
            except Exception:
                self.logger.exception("Libby transport shutdown failed")

    def payload(self, value: Any = None, /, **extra: Any) -> dict:
        """Normalize a user result into a JSON-serializable dictionary."""

        if value is None:
            out: Dict[str, Any] = {}
        elif is_dataclass(value):
            out = asdict(value)
        elif isinstance(value, cabc.Mapping):
            out = dict(value)
        else:
            out = {"data": value}

        if extra:
            out.update(extra)

        try:
            json.dumps(out)
        except TypeError as exc:
            raise ValueError(
                f"payload is not JSON-serializable: {exc}"
            ) from exc

        return out
