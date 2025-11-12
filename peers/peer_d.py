from __future__ import annotations
import argparse
from typing import Any, Dict
from libby.daemon import LibbyDaemon

Payload = Dict[str, Any]

class PeerD(LibbyDaemon):

    def ping(self, payload: Payload) -> Dict[str, Any]:
        return self.payload(ok=True, got=payload, peer=self.config_peer_id())

    def echo(self, payload: Payload) -> Dict[str, Any]:
        return self.payload(payload)

    def log(self, payload: Payload) -> None:
        print(f"[{self.config_peer_id()}] topic msg: {payload}")

    def on_start(self, _libby):
        # Map service names -> methods
        service_impls = {
            "ping": self.ping,
            "echo": self.echo,
        }
        for name in self._config.get("services", []):
            impl = service_impls.get(name)
            if impl:
                self.add_service(name, impl)

        # Subscribe to topics from config
        for topic in self._config.get("topics", []):
            self.add_topic(topic, self.log)

def main():
    ap = argparse.ArgumentParser(description="Run PeerD (config-driven)")
    ap.add_argument("-c", "--config", required=True, help="Path to JSON or YAML config")
    ns = ap.parse_args()

    PeerD.from_config_file(ns.config, env_prefix="LIBBY_").serve()

if __name__ == "__main__":
    main()
