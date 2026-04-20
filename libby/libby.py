from __future__ import annotations
from typing import Callable, Dict, List, Optional, Any
import time

from bamboo.keys import KeyRegistry
from bamboo.protocol import Protocol
from bamboo.discovery import Discovery

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
        self.proto = Protocol(transport=self.transport, self_id=self_id, keys=self.keys)

        # bamboo owns the semantics
        if keys:
            for k in keys:
                self.proto.serve(k, callback if callback else (lambda *_: None))

        # Periodic discovery (provided by bamboo)
        self._disco: Optional[Discovery] = None
        if discover:
            self._disco = Discovery(self.proto.send, self_id, self.keys, every_seconds=int(discover_interval_s))
            if hello_on_start:
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
        group_id: Optional[str] = None,
    ) -> "Libby":
        try:
            from .zmq_transport import ZmqTransport
        except Exception as e:
            raise RuntimeError(
                "ZMQ transport not available. Install optional deps:\n"
                "  pip install libby[zmq]\n"
                "or provide your own Transport implementation."
            ) from e

        t = ZmqTransport(bind_router=bind, address_book=address_book, my_id=self_id, group_id=group_id)
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

    @classmethod
    def rabbitmq(
        cls,
        self_id: str,
        rabbitmq_url: str = "amqp://localhost",
        keys: Optional[List[str]] = None,
        callback: Optional[Callable[[dict, dict], Optional[dict]]] = None,
        group_id: Optional[str] = None,
    ) -> "Libby":
        """
        Create a Libby instance using RabbitMQ transport.

        Args:
            self_id: Unique identifier for this peer
            rabbitmq_url: RabbitMQ connection URL (default: "amqp://localhost")
            keys: List of RPC keys this peer will serve
            callback: Default callback for RPC requests
            group_id: Optional group identifier

        Returns:
            Configured Libby instance

        Example:
            >>> libby = Libby.rabbitmq(
            ...     self_id="peer-A",
            ...     rabbitmq_url="amqp://user:pass@localhost:5672/",
            ...     keys=["echo"],
            ...     group_id="hsfei"
            ... )
        """
        try:
            from .rabbitmq_transport import RabbitMQTransport
        except Exception as e:
            raise RuntimeError(
                "RabbitMQ transport not available. Install optional deps:\n"
                "  pip install libby[rabbitmq]\n"
                "or:\n"
                "  pip install pika"
            ) from e

        t = RabbitMQTransport(peer_id=self_id, rabbitmq_url=rabbitmq_url, group_id=group_id)
        t.start()
        return cls(
            self_id=self_id,
            transport=t,
            keys=keys,
            callback=callback,
            discover=False,
            discover_interval_s=0,
            hello_on_start=False,
        )

    # lifecycle
    def start(self) -> None:
        if hasattr(self.transport, "start"):
            try: self.transport.start()
            except Exception: pass

    def stop(self) -> None:
        if getattr(self, "_disco", None):
            try: self._disco.stop()
            except Exception: pass
        if hasattr(self.transport, "stop"):
            try: self.transport.stop()
            except Exception: pass

    close = stop

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.stop()

    # thin passthroughs to bamboo.Protocol
    def request(self, peer_id: str, key: str, payload: Dict[str, Any], ttl_ms: int = 8000):
        return self.proto.request_peer(peer_id, key, payload, timeout_s=ttl_ms / 1000.0)

    def rpc(self, peer_id: str, key: str, payload: Dict[str, Any], ttl_ms: int = 8000):
        return self.request(peer_id, key, payload, ttl_ms)

    def serve_keys(self, keys: List[str], callback: Callable[[dict, dict], Optional[dict]]) -> None:
        for k in keys:
            self.proto.serve(k, callback)

    def listen(self, topic: str, handler: Callable[[Any], None]) -> None:
        self.proto.listen(topic, handler)

    def listen_many(self, handlers: Dict[str, Callable[[Any], None]]) -> None:
        for topic, h in handlers.items():
            self.listen(topic, h)

    def publish(self, topic: str, payload: Dict[str, Any]) -> int:
        return self.proto.publish(topic, payload)

    def emit(self, topic: str, payload: Dict[str, Any]) -> int:
        return self.publish(topic, payload)

    def subscribe(self, *topics: str) -> None:
        self.proto.subscribe_topics(add=list(topics))

    def unsubscribe(self, *topics: str) -> None:
        self.proto.subscribe_topics(remove=list(topics))

    def hello(self) -> None:
        try:
            self.proto.announce_hello()
        except AttributeError:
            pass

    def peers_alive(self, within_s: int = 30) -> Dict[str, float]:
        if hasattr(self.proto, "peers"):
            return self.proto.peers.alive(within_s)
        return {}

    def knows_key(self, peer_id: str, key: str) -> bool:
        return self.proto.keys.peer_supports(peer_id, key)

    def wait_for_key(self, peer_id: str, key: str, timeout_s: float = 3.0, poll_s: float = 0.05) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.knows_key(peer_id, key):
                return True
            time.sleep(poll_s)
        return False

    def wait_for_peer(self, peer_id: str, timeout_s: float = 3.0, poll_s: float = 0.05) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if peer_id in self.peers_alive(within_s=9999):
                return True
            time.sleep(poll_s)
        return False

    def learn_peer_keys(self, peer_id: str, keys: List[str]) -> None:
        self.proto.learn_peer_keys(peer_id, keys)

    def run_forever(self) -> None:
        try:
            while True: time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()
