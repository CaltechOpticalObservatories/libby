from __future__ import annotations
import json
from dataclasses import is_dataclass, asdict
import collections.abc as cabc
import signal, sys, threading, time
from typing import Any, Callable, Dict, List, Optional
from .libby import Libby

Payload = Dict[str, Any]
RPCHandler = Callable[[Payload], Dict[str, Any]]
EvtHandler = Callable[[Payload], None]

class LibbyDaemon:
    # simple attributes users set
    peer_id: Optional[str] = None
    bind: Optional[str] = None
    address_book: Optional[Dict[str, str]] = None
    discovery_enabled: bool = True
    discovery_interval_s: float = 5.0

    # payload-only handlers
    services: Dict[str, RPCHandler] = {}
    topics: Dict[str, EvtHandler] = {}

    # optional hooks
    def on_start(self, libby: Libby) -> None: ...
    def on_stop(self) -> None: ...
    def on_hello(self, libby: Libby) -> None: ...
    def on_event(self, topic: str, msg) -> None:
        print(f"[{self.__class__.__name__}] {topic}: {msg.env.payload}")

    def config_peer_id(self) -> str: return self.peer_id or self._must("peer_id")
    def config_bind(self) -> str: return self.bind or self._must("bind")
    def config_address_book(self) -> Dict[str, str]: return self.address_book or self._must("address_book")
    def config_discovery_enabled(self) -> bool: return bool(self.discovery_enabled)
    def config_discovery_interval_s(self) -> float: return float(self.discovery_interval_s)
    def config_rpc_keys(self) -> List[str]: return list(self.services.keys())
    def config_subscriptions(self) -> List[str]: return list(self.topics.keys())

    # user-facing helpers
    def add_service(self, key: str, fn: RPCHandler) -> None:
        self.services[key] = fn
        if hasattr(self, "libby"): self._register_services({key: fn})

    def add_services(self, mapping: Dict[str, RPCHandler]) -> None:
        self.services.update(mapping)
        if hasattr(self, "libby"): self._register_services(mapping)

    def add_topic(self, topic: str, fn: EvtHandler) -> None:
        self.topics[topic] = fn
        if hasattr(self, "libby"):
            self.libby.listen(topic, lambda msg, _h=fn: _h(msg.env.payload))
            self.libby.subscribe(topic)

    def add_topics(self, mapping: Dict[str, EvtHandler]) -> None:
        self.topics.update(mapping)
        if hasattr(self, "libby"):
            for topic, fn in mapping.items():
                self.libby.listen(topic, lambda msg, _h=fn: _h(msg.env.payload))
            self.libby.subscribe(*mapping.keys())

    # internals
    def _must(self, name: str):
        raise NotImplementedError(f"Set `{name}` or override config_{name}()")

    def _service_adapter(self, fn):
        def adapter(user_payload: dict, _ctx: dict) -> dict:
            try:
                result = fn(user_payload)      # user returns ANYTHING
                return self.payload(result)    # we "shove it into payload" for them
            except Exception as ex:
                return {"ok": False, "error": str(ex)}
        return adapter

    def _register_services(self, mapping: Dict[str, RPCHandler]) -> None:
        for key, fn in mapping.items():
            self.libby.serve_keys([key], self._service_adapter(fn))

    def build_libby(self) -> Libby:
        return Libby.zmq(
            self_id=self.config_peer_id(),
            bind=self.config_bind(),
            address_book=self.config_address_book(),
            keys=[], callback=None,                     # register per-key
            discover=self.config_discovery_enabled(),
            discover_interval_s=self.config_discovery_interval_s(),
            hello_on_start=True,
        )

    def serve(self) -> None:
        stop_evt = threading.Event()
        def _sig(_s, _f): stop_evt.set()
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        try:
            self.libby = self.build_libby()
        except Exception as ex:
            print(f"[{self.__class__.__name__}] failed to start: {ex}", file=sys.stderr)
            raise

        if self.services:
            self._register_services(self.services)
        if self.topics:
            for topic, fn in self.topics.items():
                self.libby.listen(topic, lambda msg, _h=fn: _h(msg.env.payload))
            self.libby.subscribe(*self.topics.keys())

        # discovery hello + hooks
        try:
            if self.config_discovery_enabled():
                self.libby.hello()
                self.on_hello(self.libby)
        except Exception:
            pass

        try:
            self.on_start(self.libby)
        except Exception as ex:
            print(f"[{self.__class__.__name__}] on_start error: {ex}", file=sys.stderr)

        print(f"[{self.__class__.__name__}] up: id={self.config_peer_id()} bind={self.config_bind()}")
        try:
            while not stop_evt.is_set(): time.sleep(0.5)
        finally:
            try: self.on_stop()
            except Exception: pass
            self.libby.stop()
            print(f"[{self.__class__.__name__}] stopped")

    def payload(self, value=None, /, **extra) -> dict:
        if value is None:
            out = {}
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
        except TypeError as e:
            raise ValueError(f"Payload not JSON-serializable: {e}") from e

        return out

