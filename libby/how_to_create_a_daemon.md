# How to Build a Peer with `LibbyDaemon`

This guide shows you how to create a  peer using the **Libby** and the **`LibbyDaemon`** base class. You’ll wire a transport, discovery, RPC handlers, and pub/sub with just a few overrides.

> Libby is transport-agnostic. Examples below use **ZMQ** transport.

---


## 1) What `LibbyDaemon` gives you

- **Lifecycle**: easy start/stop, and proper termination handling
- **Transport**: default `Libby.zmq(...)` factory (or supply your own transport)
- **Discovery**: optional periodic HELLO 
- **RPC (REQ/RESP)**: register keys and handle requests in one method
- **Pub/Sub (PUB)**: register event listeners and subscribe to topics

You subclass `LibbyDaemon`, override a few config methods and hooks then call `.serve()`.

---

## 2) Extending the daemon base class

```python
# from libby.daemon import LibbyDaemon

class LibbyDaemon:
    # Config (required)
    def config_peer_id(self) -> str: ...
    def config_bind(self) -> str: ...
    def config_address_book(self) -> dict[str, str]: ...

    # Optional config
    def config_discovery_enabled(self) -> bool: return True
    def config_discovery_interval_s(self) -> float: return 5.0
    def config_rpc_keys(self) -> list[str]: return []
    def config_subscriptions(self) -> list[str]: return []

    # Hooks
    def on_req(self, key, payload, ctx) -> dict | None: ...
    def on_event(self, topic, msg) -> None: ...
    def on_start(self, libby) -> None: ...
    def on_stop(self) -> None: ...
    def on_hello(self, libby) -> None: ...
```
---

## 3) A simple peer

Serves one RPC key `perf.echo` and subscribes to `alerts.status`.

```python
import time
from typing import Dict, Any
from libby.daemon import LibbyDaemon

class PeerB(LibbyDaemon):
    # Config
    def config_peer_id(self): return "peer-B"
    def config_bind(self):    return "tcp://*:5556"
    def config_address_book(self):
        return {"peer-A": "tcp://127.0.0.1:5555"}

    # Optional config
    def config_discovery_enabled(self): return True
    def config_discovery_interval_s(self): return 2.0
    def config_subscriptions(self): return ["alerts.status"]
    def config_rpc_keys(self): return ["perf.echo"]

    # RPC handler
    def on_req(self, key: str, payload: Dict[str, Any], ctx: Dict[str, Any]):
        if key == "perf.echo":
            t0 = payload.get("t0")
            return {"ok": True, "t0": t0, "t1": time.time(), "from": ctx["source"]}
        return {"ok": False, "error": f"unknown key {key}"}

    # Event handler
    def on_event(self, topic: str, msg):
        print(f"[PeerB] {topic} -> {msg.env.payload}")

if __name__ == "__main__":
    PeerB().serve()
```

Run:

```bash
python peer_b.py
```

---

## 4) Another simple peer

Sends one REQ to `peer-B` and publishes a status event. Uses discovery.

```python
import time
from libby.daemon import LibbyDaemon

class PeerA(LibbyDaemon):
    def config_peer_id(self): return "peer-A"
    def config_bind(self):    return "tcp://*:5555"
    def config_address_book(self): return {"peer-B": "tcp://127.0.0.1:5556"}
    def config_discovery_enabled(self): return True
    def config_discovery_interval_s(self): return 2.0
    def config_subscriptions(self): return ["alerts.status"]

    def on_start(self, libby):
        # Wait a moment for discovery
        try:
            if not libby.wait_for_key("peer-B", "perf.echo", timeout_s=3.0):
                libby.learn_peer_keys("peer-B", ["perf.echo"])
        except AttributeError:
            pass

        print("[PeerA] sending perf.echo to peer-B …")
        res = libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=8000)
        print("[PeerA] result:", res)

        sent = libby.emit("alerts.status", {"source": "peer-A", "ok": True})
        print(f"[PeerA] alerts.status emitted (direct={sent})")

if __name__ == "__main__":
    PeerA().serve()
```

Run:

```bash
python peer_a.py
```

---

## 5) Multi-Key Client (Peer C), kept simple

```python
import time
from typing import Dict, Any, Callable
from libby.daemon import LibbyDaemon

class PeerC(LibbyDaemon):
    def config_peer_id(self): return "peer-C"
    def config_bind(self):    return "tcp://*:5557"
    def config_address_book(self):
        return {"peer-A": "tcp://127.0.0.1:5555", "peer-B": "tcp://127.0.0.1:5556"}
    def config_discovery_enabled(self): return True
    def config_discovery_interval_s(self): return 2.0
    def config_subscriptions(self): return ["alerts.status"]

    def config_rpc_keys(self):
        return list(self._handlers().keys())

    def _handlers(self) -> Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]]:
        return {
            "peerC.info":      self._info,
            "math.add":        self._math_add,
            "perf.echo.proxy": self._echo_proxy,
        }

    def on_req(self, key, payload, ctx):
        fn = self._handlers().get(key)
        if not fn:
            return {"ok": False, "error": f"unknown key '{key}'"}
        out = fn(payload, ctx)
        return out if ("ok" in out or "error" in out) else {"ok": True, "data": out}

    def _info(self, payload, ctx):
        return {"ok": True, "peer": self.config_peer_id(), "time": time.time()}

    def _math_add(self, payload, ctx):
        a, b = payload.get("a"), payload.get("b")
        if not isinstance(a, (int,float)) or not isinstance(b, (int,float)):
            return {"ok": False, "error": "need numeric a,b"}
        return {"ok": True, "sum": a + b}

    def _echo_proxy(self, payload, ctx):
        res = self.libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=6000)
        return {"ok": True, "forwarded_to": "peer-B", "result": res}

    def on_start(self, libby):
        print("[PeerC] math.add(2,5) ->",
              libby.rpc(self.config_peer_id(), "math.add", {"a":2,"b":5}, ttl_ms=2000))
        sent = self.libby.emit("alerts.status", {"source": "peer-C", "ok": True})
        print(f"[PeerC] alerts.status emitted (direct={sent})")

if __name__ == "__main__":
    PeerC().serve()
```

## 6) Design Notes

- **No retries** (protocol choice): sender waits for ACK and optionally RESP; you can handle retries at the app level if needed.
- **Transport-agnostic**: ZMQ is a transport implementing bamboo’s `Transport`.
- **Encapsulation goal**: application peers only implement business logic (`on_req`, `on_event`) and a few config methods.

