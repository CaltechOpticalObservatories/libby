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
        "peer-C": "tcp://127.0.0.1:5557"
    }
    discovery_enabled = True
    discovery_interval_s = 2.0

    services = {
        "clientC.info": info,
        "math.add":     math_add,
    }

    def on_start(self, libby):
        # Proxy service
        def echo_proxy(_p: Dict[str, Any]):
            res = libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=6000)
            # returning a dict
            return {"ok": True, "forwarded_to": "peer-B", "result": res}

        # Register at runtime
        self.add_service("perf.echo.proxy", echo_proxy)

        print("[PeerC] math.add(2,5) ->",
              libby.rpc(self.peer_id, "math.add", {"a": 2, "b": 5}, ttl_ms=2000))
        libby.publish("alerts.status", {"source": "peer-C", "ok": True})

if __name__ == "__main__":
    PeerC().serve()
