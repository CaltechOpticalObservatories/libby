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

        # publish a status
        libby.publish("alerts.status", {"source": "peer-A", "ok": True})
        print("[PeerA] published alerts.status")

if __name__ == "__main__":
    PeerA().serve()
