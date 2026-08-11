import time

from libby.daemon import LibbyDaemon


class PeerA(LibbyDaemon):
    peer_id = "peer-A"

    bind = "tcp://*:5555"
    address_book = {
        "peer-B": "tcp://127.0.0.1:5556",
        "peer-C": "tcp://127.0.0.1:5557",
        "cli": "tcp://127.0.0.1:56001",
    }

    transport = "zmq"
    discovery_enabled = True
    discovery_interval_s = 2.0

    def on_start(self, libby):
        try:
            if not libby.wait_for_key(
                "peer-B",
                "perf.echo",
                timeout_s=2.5,
            ):
                libby.learn_peer_keys(
                    "peer-B",
                    ["perf.echo", "ping.txt", "answer"],
                )

            print("[PeerA] asking B: perf.echo ...")
            result = libby.rpc(
                "peer-B",
                "perf.echo",
                {"t0": time.time()},
                ttl_ms=8000,
            )
            print("[PeerA] result:", result)

        except Exception as exc:
            print(f"[PeerA] Peer B request failed: {exc}")

        libby.publish(
            "alerts.status",
            {
                "source": self.peer_id,
                "ok": True,
                "timestamp": time.time(),
            },
        )
        print("[PeerA] published alerts.status")


if __name__ == "__main__":
    PeerA().serve()
