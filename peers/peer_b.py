import time
from typing import Dict, Any
from libby.daemon import LibbyDaemon

def handle_echo(p: Dict[str, Any]):
    # can return any JSON-serializable thing
    return {"ok": True, "t0": p.get("t0"), "t1": time.time()}

def handle_ping(_p):
    # returns a string
    return "pong"

def handle_answer(_p):
    # returns a number
    return 42

def on_status(payload: Dict[str, Any]) -> None:
    print("[PeerB] alerts.status:", payload)

class PeerB(LibbyDaemon):
    peer_id = "peer-B"
    bind = "tcp://*:5556"
    address_book = {
        "peer-A": "tcp://127.0.0.1:5555",
        "peer-C": "tcp://127.0.0.1:5557",
        "peer-D": "tcp://127.0.0.1:5558",
    }
    discovery_enabled = True
    discovery_interval_s = 2.0

    services = {
        "perf.echo": handle_echo,
        "ping.txt":  handle_ping,
        "answer":    handle_answer,
    }

    # Topics (PUB/SUB)
    topics = {
        "alerts.status": on_status,
    }

if __name__ == "__main__":
    PeerB().serve()
