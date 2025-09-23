from __future__ import annotations
from typing import Callable, Dict, List, Optional, Any
import time
from bamboo.keys import KeyRegistry
from bamboo.protocol import Protocol
from bamboo.discovery import Discovery
from .zmq_transport import ZmqTransport


class Libby:
    def __init__(
        self,
        self_id: str,
        transport,
        keys: Optional[List[str]] = None,
        callback: Optional[Callable[[dict, dict], Optional[dict]]] = None,
        *,
        discover: bool = False,
        discover_interval_s: float = 5.0,
        hello_on_start: bool = True,
    ):
        self.self_id = self_id
        self.transport = transport
        self.keys = KeyRegistry()
        self.proto = Protocol(transport=self.transport,
                              self_id=self_id, keys=self.keys)

        if keys:
            for k in keys:
                self.proto.serve(
                    k, callback if callback else (lambda *_: None))

        # Start periodic discovery if requested
        self._disco: Optional[Discovery] = None
        if discover:
            self._disco = Discovery(
                self.proto.send, self_id, self.keys, every_seconds=int(discover_interval_s))
            if hello_on_start:
                # Try Discovery.announce_now(); if not present, fall back to Protocol.announce_hello()
                try:
                    self._disco.announce_now()
                except AttributeError:
                    try:
                        self.proto.announce_hello()
                    except AttributeError:
                        pass
            self._disco.start()

    @classmethod
    def zmq(
        cls,
        self_id: str,
        bind: str,
        address_book: Dict[str, str],
        keys: Optional[List[str]] = None,
        callback: Optional[Callable[[dict, dict], Optional[dict]]] = None,
        *,
        discover: bool = False,
        discover_interval_s: float = 5.0,
        hello_on_start: bool = True,
    ) -> "Libby":
        t = ZmqTransport(bind_router=bind,
                         address_book=address_book, my_id=self_id)
        t.start()
        return cls(
            self_id=self_id,
            transport=t,
            keys=keys,
            callback=callback,
            discover=discover,
            discover_interval_s=discover_interval_s,
            hello_on_start=hello_on_start,
        )

    def stop(self) -> None:
        if getattr(self, "_disco", None):
            try:
                self._disco.stop()
            except Exception:
                pass
        if hasattr(self.transport, "stop"):
            try:
                self.transport.stop()
            except Exception:
                pass

    def run_forever(self) -> None:
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()

    # Context manager (so `with Libby.zmq(...) as libby:` works)
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def request(self, peer_id: str, key: str, payload: Dict[str, Any], ttl_ms: int = 8000):
        return self.proto.request_peer(peer_id, key, payload, timeout_s=ttl_ms / 1000.0)

    # Pub/Sub
    def listen(self, topic: str, handler: Callable[[Any], None]) -> None:
        self.proto.listen(topic, handler)

    def publish(self, topic: str, payload: Dict[str, Any]) -> int:
        return self.proto.publish(topic, payload)

    def subscribe(self, *topics: str) -> None:
        self.proto.subscribe_topics(add=list(topics))

    def unsubscribe(self, *topics: str) -> None:
        self.proto.subscribe_topics(remove=list(topics))

    # Discovery helpers
    def hello(self) -> None:
        try:
            self.proto.announce_hello()
        except AttributeError:
            pass

    def learn_peer_keys(self, peer_id: str, keys: List[str]) -> None:
        self.proto.learn_peer_keys(peer_id, keys)
