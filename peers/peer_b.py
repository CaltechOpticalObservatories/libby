import time
from libby import Libby

PEER_ID = "peer-B"
BIND = "tcp://*:5556"
ADDRESS_BOOK = {
    "peer-A": "tcp://127.0.0.1:5555",
}

def handle_echo(payload, ctx):
    """
    Handler for key 'perf.echo'
    """
    t0 = payload.get("t0")
    return {"ok": True, "t1": time.time(), "t0": t0, "from": ctx["source"]}

def on_alert(msg):
    """
    Event handler for PUB topic 'alerts.status'
    """
    print(f"[PeerB] alerts.status event: {msg.env.payload}")

def main():
    libby = Libby.zmq(
        self_id=PEER_ID,
        bind=BIND,
        address_book=ADDRESS_BOOK,
        keys=["perf.echo"],
        callback=handle_echo,
        discover=True,               
        discover_interval_s=2.0,
        hello_on_start=True,
    )

    # Subscribe to the topic and register an event handler
    libby.listen("alerts.status", on_alert)
    libby.subscribe("alerts.status")   # announces interest to peers

    print(f"[PeerB] up on {BIND}; serving 'perf.echo' and subscribed to 'alerts.status'")
    libby.run_forever()

if __name__ == "__main__":
    main()
