import time
from libby import Libby

PEER_ID = "peer-A"
BIND = "tcp://*:5555"
ADDRESS_BOOK = {
    "peer-B": "tcp://127.0.0.1:5556",
}

def main():
    # Start with discovery enabled (HELLO broadcasts caps/keys/subs)
    with Libby.zmq(
        self_id=PEER_ID,
        bind=BIND,
        address_book=ADDRESS_BOOK,
        discover=True,
        discover_interval_s=2.0,   # faster announcements during dev
        hello_on_start=True,
    ) as libby:

        # Wait (briefly) until we learn PeerB serves 'perf.echo'
        libby.learn_peer_keys("peer-B", ["perf.echo"])

        print("[PeerA] sending REQ perf.echo to peer-B...")
        result = libby.request("peer-B", key="perf.echo", payload={"t0": time.time()}, ttl_ms=8000)
        print("[PeerA] result:", result)

        # Publish a status event
        sent = libby.publish("alerts.status", {"ok": True})
        if sent:
            print(f"[PeerA] published alerts.status to {sent} subscriber(s)")
        else:
            print("[PeerA] broadcasted alerts.status")

if __name__ == "__main__":
    main()
