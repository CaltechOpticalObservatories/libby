# How to Build a Peer with `LibbyDaemon`

This guide shows you how to create a peer using the **Libby** and the **`LibbyDaemon`** base class. You'll wire a transport, discovery, RPC handlers, and pub/sub with just a few overrides.

> Libby is transport-agnostic. Examples below show **ZMQ** (peer-to-peer), **RabbitMQ** (brokered) and **Cyclone DDS** (peer-to-peer with discovery) transports.

---

## Install (dev)

```bash
python -m venv .venv
source .venv/bin/activate

# editable install
pip install -e .
```

For RabbitMQ transport, you'll also need a RabbitMQ server running:

```bash
# macOS with Homebrew
brew install rabbitmq
brew services start rabbitmq

# Ubuntu/Debian
sudo apt-get install rabbitmq-server
sudo systemctl start rabbitmq-server
```

---

## 1) What `LibbyDaemon` gives you

- **Lifecycle**: easy start/stop, and proper termination handling
- **Transport**: choose ZMQ (peer-to-peer), RabbitMQ (brokered) or Cyclone DDS (peer-to-peer with discovery) with a single line
- **Discovery**: optional periodic HELLO
- **RPC (REQ/RESP)**: register keys and handle requests in one method
- **Pub/Sub (PUB)**: register event listeners and subscribe to topics

You subclass `LibbyDaemon`, override a few config methods and hooks then call `.serve()`.

---

## 2) Extending the daemon base class

### ZMQ Transport (default - peer-to-peer)

```python
from libby.daemon import LibbyDaemon

class MyPeer(LibbyDaemon):
    # Required
    peer_id = "peer-X"

    # ZMQ config (default transport)
    transport = "zmq"  # optional, this is the default
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

    def on_start(self, libby):
        # Manually teach peers about each other (discovery workaround)
        libby.learn_peer_keys("peer-Y", ["service1", "service2"])

if __name__ == "__main__":
    MyPeer().serve()
```

### RabbitMQ Transport (brokered)

```python
from libby.daemon import LibbyDaemon

class MyPeer(LibbyDaemon):
    # Required
    peer_id = "peer-X"

    # RabbitMQ config
    transport = "rabbitmq"
    rabbitmq_url = "amqp://localhost"  # or "amqp://user:pass@host:5672/"
    group_id = "workers"  # optional

    # No bind or address_book needed - broker handles routing!

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

    def on_start(self, libby):
        # Manually teach peers about each other (discovery workaround)
        libby.learn_peer_keys("peer-Y", ["service1", "service2"])

if __name__ == "__main__":
    MyPeer().serve()
```

**Note:** Currently, you need to manually specify peer keys using `learn_peer_keys()`. Automatic discovery is not yet fully working with either transport.

### Cyclone DDS Transport (peer-to-peer, discovered)

```python
from libby.daemon import LibbyDaemon

class MyPeer(LibbyDaemon):
    peer_id = "peer-X"
    group_id = "workers"

    # Cyclone DDS config; needs the cyclonedds extra
    transport = "cyclonedds"
    dds_domain_id = 0       # only peers on the same domain see each other
    dds_config = None       # Cyclone XML or a path/URI to it; None reads CYCLONEDDS_URI

    # No bind, address_book or broker: DDS discovery finds the peers

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

## 6) Transport Selection

Libby supports three transports that you can switch between with a single line:

### ZMQ Transport (default)
- **Peer-to-peer**: Direct socket connections between peers
- **Requires**: `bind` address and `address_book` with peer locations
- **Best for**: Low-latency, direct communication, no infrastructure
- **Setup**: No server needed, just run the peers

### RabbitMQ Transport
- **Brokered**: All messages go through RabbitMQ server
- **Requires**: RabbitMQ server running, `rabbitmq_url`
- **No address book needed**: Broker handles all routing automatically
- **Best for**: Dynamic scaling, easier deployment, built-in monitoring
- **Setup**: Install and start RabbitMQ server

### Cyclone DDS Transport
- **Peer-to-peer with discovery**: messages go straight between peers, which find each other through DDS discovery
- **Requires**: the `cyclonedds` extra; optionally `dds_domain_id` and `dds_config`
- **No address book or broker**: a direct message is routed by its destination's DDS partition
- **Best for**: many peers and high request rates without a broker in the path
- **Setup**: `pip install libby[cyclonedds]`; on networks that block multicast, list unicast peers in the Cyclone XML (`dds_config` or `CYCLONEDDS_URI`)

**To switch transports**: Just change `transport = "zmq"` to `transport = "rabbitmq"` or `transport = "cyclonedds"` in your peer class. Everything else stays the same!

### Group ID (optional)

Optional `group_id` for grouping peers:

```python
class MyPeer(LibbyDaemon):
    peer_id = "worker-1"
    transport = "zmq"  # or "rabbitmq" / "cyclonedds"
    group_id = "workers"    # group this peer belongs to
```

- **RabbitMQ**: The group ID is included in the queue name (e.g., `libby.group.workers.peer.worker-1`) and enables group-aware features.
- **ZMQ**: The group ID is stored as metadata and can be used for group-aware routing functions (turn on all devices in a group).
- **Cyclone DDS**: The group ID is part of the peer's wire id (`workers.worker-1`), which names its DDS partition.

---

## 7) Design Notes

- **No retries** (protocol choice): sender waits for ACK and optionally RESP; you can handle retries at the app level if needed.
- **Transport-agnostic**: ZMQ, RabbitMQ and Cyclone DDS implement Bamboo's `Transport` interface (Cyclone DDS with `pack_frames = False`, carrying the envelope as typed DDS fields). The same peer code works with any transport.
- **Encapsulation goal**: application peers only implement business logic (`on_req`, `on_event`) and a few config methods.
- **Handlers are payload-only.** They receive a Python dict and return anything JSON-serializable.
  If the return is not a dict, LibbyDaemon auto-wraps it as `{"data": <value>}`.
- **Discovery workaround**: Currently, automatic peer discovery needs manual key learning via `libby.learn_peer_keys()` in `on_start()`.

