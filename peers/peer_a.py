import time
from libby.daemon import LibbyDaemon

class PeerA(LibbyDaemon):
    def config_peer_id(self): return "peer-A"
    def config_bind(self):    return "tcp://*:5555"
    def config_address_book(self): return {"peer-B": "tcp://127.0.0.1:5556"}
    def config_subscriptions(self): return ["alerts.status"]

    def on_start(self, libby):
        # Wait briefly for B to advertise the RPC key; fall back if slow
        try:
            if not libby.wait_for_key("peer-B", "perf.echo", timeout_s=3.0):
                libby.learn_peer_keys("peer-B", ["perf.echo"])
        except AttributeError:
            pass

        print("[ClientDaemon] sending perf.echo…")
        res = libby.rpc("peer-B", "perf.echo", {"t0": time.time()}, ttl_ms=8000)
        print("[ClientDaemon] resp:", res)

        sent = libby.emit("alerts.status", {"ok": True})
        print(f"[ClientDaemon] status emitted (direct={sent})")

if __name__ == "__main__":
    PeerA().serve()
