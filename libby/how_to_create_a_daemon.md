# How to Build a Peer with `LibbyDaemon`

This guide shows you how to create a  peer using the **Libby** and the **`LibbyDaemon`** base class. You’ll wire a transport, discovery, RPC handlers, and pub/sub with just a few overrides.

> Libby is transport-agnostic. Examples below use **ZMQ** transport.

---

## Install (dev)

```bash
python -m venv .venv
source .venv/bin/activate

# editable install
pip install -e .[zmq]
```
If you do not need the ZMQ transport, you can just run:
```bash
pip install -e .
```

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
from libby.daemon import LibbyDaemon

class MyPeer(LibbyDaemon):
    # Required
    peer_id = "peer-X"
    bind = "tcp://*:5555"
    address_book = {
        "peer-Y": "tcp://127.0.0.1:5556",
    }

    # Optional
    discovery_enabled = True
    discovery_interval_s = 5.0

    # REQ/RESP
    services = {
        "my.service": lambda p: {"ok": True, "echo": p},
    }

    # PUB/SUB 
    topics = {
        "alerts.status": lambda payload: print("[X] status:", payload),
    }

if __name__ == "__main__":
    MyPeer().serve()
```
---

## 3) A simple peer

Serves one RPC key `perf.echo` and subscribes to `alerts.status`.

```python
import time
from typing import Dict, Any
from libby.daemon import LibbyDaemon

def handle_echo(p: Dict[str, Any]):
    # dict return
    return {"ok": True, "t0": p.get("t0"), "t1": time.time()}

def handle_ping(_p):
    # string
    return "pong"

def handle_answer(_p):
    # number
    return 42

def on_status(payload: Dict[str, Any]) -> None:
    print("[PeerB] alerts.status:", payload)

class PeerB(LibbyDaemon):
    peer_id = "peer-B"
    bind = "tcp://*:5556"
    address_book = {
        "peer-A": "tcp://127.0.0.1:5555",
        "peer-C": "tcp://127.0.0.1:5557",
    }
    discovery_enabled = True
    discovery_interval_s = 2.0

    services = {
        "perf.echo": handle_echo,
        "ping.txt":  handle_ping,
        "answer":    handle_answer,
    }
    topics = {
        "alerts.status": on_status,
    }

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
    peer_id = "peer-A"
    bind = "tcp://*:5555"
    address_book = {
        "peer-B": "tcp://127.0.0.1:5556",
        "peer-C": "tcp://127.0.0.1:5557",
    }
    discovery_enabled = True
    discovery_interval_s = 2.0

    def on_start(self, libby):
        try:
            if not libby.wait_for_key("peer-B", "perf.echo", timeout_s=2.5):
                libby.learn_peer_keys("peer-B", ["perf.echo", "ping.txt", "answer"])
        except AttributeError:
            pass

        print("[PeerA] asking B: perf.echo …")
        res = libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=8000)
        print("[PeerA] result:", res)

        libby.publish("alerts.status", {"source": "peer-A", "ok": True})
        print("[PeerA] published alerts.status")

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
from typing import Dict, Any
from libby.daemon import LibbyDaemon

def info(_p: Dict[str, Any]):
    return {"ok": True, "info": "peer-C", "time": time.time()}

def math_add(p: Dict[str, Any]):
    a, b = p.get("a"), p.get("b")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return {"ok": False, "error": "need numeric a and b"}
    return {"ok": True, "sum": a + b}

class PeerC(LibbyDaemon):
    peer_id = "peer-C"
    bind = "tcp://*:5557"
    address_book = {
        "peer-A": "tcp://127.0.0.1:5555",
        "peer-B": "tcp://127.0.0.1:5556",
    }
    discovery_enabled = True
    discovery_interval_s = 2.0

    services = {
        "clientC.info": info,
        "math.add":     math_add,
    }

    def on_start(self, libby):
        # Proxy service needing live libby handle
        def echo_proxy(_p: Dict[str, Any]):
            res = libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=6000)
            return {"ok": True, "forwarded_to": "peer-B", "result": res}

        self.add_service("perf.echo.proxy", echo_proxy)

        print("[PeerC] math.add(2,5) ->",
              libby.rpc(self.peer_id, "math.add", {"a": 2, "b": 5}, ttl_ms=2000))

        libby.publish("alerts.status", {"source": "peer-C", "ok": True})

if __name__ == "__main__":
    PeerC().serve()
```

## 6) Design Notes

- **No retries** (protocol choice): sender waits for ACK and optionally RESP; you can handle retries at the app level if needed.
- **Transport-agnostic**: ZMQ is a transport implementing bamboo’s `Transport`.
- **Encapsulation goal**: application peers only implement business logic (`on_req`, `on_event`) and a few config methods.
- **Handlers are payload-only.** They receive a Python dict and return anything JSON-serializable.  
  If the return is not a dict, LibbyDaemon auto-wraps it as `{"data": <value>}`.

