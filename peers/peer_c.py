from libby.daemon import LibbyDaemon


class PeerC(LibbyDaemon):
    peer_id = "peer-C"

    bind = "tcp://*:5557"
    address_book = {
        "peer-A": "tcp://127.0.0.1:5555",
        "peer-B": "tcp://127.0.0.1:5556",
        "cli": "tcp://127.0.0.1:56001",
    }

    transport = "zmq"
    discovery_enabled = True
    discovery_interval_s = 2.0

    def __init__(self):
        super().__init__()

        self.last_status = None
        self.add_topic("alerts.status", self.handle_status)
        self.add_service("alerts.last", self.get_last_status)

    def handle_status(self, payload):
        self.last_status = payload
        print("[PeerC] received alerts.status:", payload)

    def get_last_status(self, payload):
        return {
            "ok": True,
            "status": self.last_status,
        }

    def on_start(self, libby):
        print("[PeerC] listening for alerts.status")


if __name__ == "__main__":
    PeerC().serve()
