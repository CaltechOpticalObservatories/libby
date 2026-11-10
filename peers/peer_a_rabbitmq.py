import time
from libby.daemon import LibbyDaemon

class PeerA(LibbyDaemon):
    peer_id = "peer-A"
    transport = "rabbitmq"
    rabbitmq_url = "amqp://localhost"

    def on_start(self):
        # Give peer B a moment to register its services
        time.sleep(1.5)

        print("[PeerA] asking B: perf.echo …")
        res = self.libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=8000)
        print("[PeerA] result:", res)

        # publish a status
        self.libby.publish("alerts.status", {"source": "peer-A", "ok": True})
        print("[PeerA] published alerts.status")

if __name__ == "__main__":
    PeerA().serve()
