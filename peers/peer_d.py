from __future__ import annotations
import argparse, time
from typing import Any, Dict
from libby.daemon import LibbyDaemon

Payload = Dict[str, Any]

class PeerD(LibbyDaemon):
    def ping(self, payload: Payload) -> Dict[str, Any]:
        return self.payload(ok=True, got=payload, peer=self.config_peer_id())

    def echo(self, payload: Payload) -> Dict[str, Any]:
        return self.payload(payload)

    def on_start(self, libby):
        import time
        target = "peer-B"
        key = "perf.echo"

        ab = self.config_address_book()
        print(f"[{self.config_peer_id()}] routes: {ab}")

        # 1) Best-effort
        libby.wait_for_peer(target, timeout_s=2.0)

        # 2) If the key isn't known yet (discovery not populated)
        if not libby.wait_for_key(target, key, timeout_s=1.0):
            try:
                libby.learn_peer_keys(target, [key])
            except Exception:
                pass

        print(f"[{self.config_peer_id()}] asking {target}: {key} …")
        res = libby.rpc(target, key, {"t0": time.time()}, ttl_ms=8000)
        print(f"[{self.config_peer_id()}] result: {res}")


def main():
    ap = argparse.ArgumentParser(description="Run PeerD (config-driven)")
    ap.add_argument("-c", "--config", required=True, help="Path to JSON or YAML config")
    ns = ap.parse_args()
    PeerD.from_config_file(ns.config).serve()

if __name__ == "__main__":
    main()
