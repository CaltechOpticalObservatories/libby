from __future__ import annotations
import signal
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Any

from .libby import Libby

class LibbyDaemon:
    """
    Base class for building Libby-based daemons with a consistent shape.

    Override the config_* methods and (optionally) the handler hooks:

    Required config (override):
      - config_peer_id() -> str
      - config_bind() -> str                 e.g., "tcp://*:5555"
      - config_address_book() -> Dict[str,str]

    Optional config (override as needed):
      - config_discovery_enabled() -> bool   (default True)
      - config_discovery_interval_s() -> float (default 5.0)
      - config_rpc_keys() -> List[str]       keys you serve for REQ/CONFIG
      - config_subscriptions() -> List[str]  PUB topics you want to receive

    Hooks (override as needed):
      - on_req(key, payload, ctx) -> dict|None
      - on_event(topic, msg) -> None
      - on_start(libby) -> None
      - on_stop() -> None
      - on_hello(libby) -> None

    Transport:
      - By default, uses Libby.zmq() based on your bind/address_book.
      - To use a custom transport, override build_libby().
    """

    # Configuration (override in subclasses)
    def config_peer_id(self) -> str:
        raise NotImplementedError

    def config_bind(self) -> str:
        raise NotImplementedError

    def config_address_book(self) -> Dict[str, str]:
        raise NotImplementedError

    def config_discovery_enabled(self) -> bool:
        return True

    def config_discovery_interval_s(self) -> float:
        return 5.0

    def config_rpc_keys(self) -> List[str]:
        return []

    def config_subscriptions(self) -> List[str]:
        return []

    # Hooks
    def on_req(self, key: str, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Default behavior: echo back what was received
        return {"ok": True, "echo": {"key": key, "payload": payload, "from": ctx.get("source")}}

    def on_event(self, topic: str, msg) -> None:
        # Default behavior: print events
        print(f"[{self.__class__.__name__}] event {topic}: {msg.env.payload}")

    def on_start(self, libby: Libby) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def on_hello(self, libby: Libby) -> None:
        pass

    # Wiring
    def _make_rpc_callback(self) -> Optional[Callable[[dict, dict], Optional[dict]]]:
        keys = self.config_rpc_keys()
        if not keys:
            return None

        def _cb(payload: dict, ctx: dict):
            # Protocol.serve() wraps into (payload, ctx); route by ctx["key"]
            return self.on_req(ctx.get("key"), payload, ctx)
        return _cb

    def build_libby(self) -> Libby:
        """
        Construct Libby. Override this if you want a custom transport.
        Default uses ZMQ with your bind/address book.
        """
        peer_id = self.config_peer_id()
        bind = self.config_bind()
        abook = self.config_address_book()

        return Libby.zmq(
            self_id=peer_id,
            bind=bind,
            address_book=abook,
            keys=self.config_rpc_keys(),
            callback=self._make_rpc_callback(),
            discover=self.config_discovery_enabled(),
            discover_interval_s=self.config_discovery_interval_s(),
            hello_on_start=True,
        )

    # Public entrypoint
    def serve(self) -> None:
        stop_evt = threading.Event()

        def _sig_handler(_signum, _frame):
            stop_evt.set()

        # Graceful shutdown
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        # Build Libby (and transport)
        try:
            self.libby = self.build_libby()
        except Exception as ex:
            print(f"[{self.__class__.__name__}] failed to start: {ex}", file=sys.stderr)
            raise

        # Register event listeners + subscribe
        subs = self.config_subscriptions()
        for topic in subs:
            # bind topic to handler; lambda captures topic as default arg
            self.libby.listen(topic, lambda msg, _t=topic: self.on_event(_t, msg))
        if subs:
            self.libby.subscribe(*subs)

        # Hook after wiring
        try:
            self.on_start(self.libby)
        except Exception as ex:
            print(f"[{self.__class__.__name__}] on_start error: {ex}", file=sys.stderr)

        # If discovery is enabled
        try:
            if self.config_discovery_enabled():
                self.libby.hello()
                self.on_hello(self.libby)
        except Exception:
            pass

        pid = self.config_peer_id()
        bind = self.config_bind()
        print(f"[{self.__class__.__name__}] up: id={pid} bind={bind}")

        # Simple keeper loop 
        try:
            while not stop_evt.is_set():
                time.sleep(0.5)
        finally:
            try:
                self.on_stop()
            except Exception:
                pass
            self.libby.stop()
            print(f"[{self.__class__.__name__}] stopped")
