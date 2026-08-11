import time

from libby.daemon import LibbyDaemon


class PeerB(LibbyDaemon):
    peer_id = "peer-B"

    bind = "tcp://*:5556"
    address_book = {
        "peer-A": "tcp://127.0.0.1:5555",
        "peer-C": "tcp://127.0.0.1:5557",
        "cli": "tcp://127.0.0.1:56001",
    }

    transport = "zmq"
    discovery_enabled = True
    discovery_interval_s = 2.0

    services = {
        "perf.echo": lambda payload: {
            "ok": True,
            "received": payload,
            "responded_at": time.time(),
        },
        "ping.txt": lambda payload: {
            "ok": True,
            "message": "pong",
        },
        "answer": lambda payload: {
            "ok": True,
            "value": 42,
        },
    }

    def on_start(self, libby):
        print("[PeerB] ready")
        print("[PeerB] services:", list(self.services))


if __name__ == "__main__":
    PeerB().serve()
