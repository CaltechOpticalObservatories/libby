import time
from typing import Dict, Any
from libby.daemon import LibbyDaemon

class PeerB(LibbyDaemon):
    def config_peer_id(self) -> str:
        return "peer-B"

    def config_bind(self) -> str:
        return "tcp://*:5556"

    def config_address_book(self) -> Dict[str, str]:
        return {"peer-A": "tcp://127.0.0.1:5555"}

    def config_rpc_keys(self):
        return ["perf.echo"]

    def config_subscriptions(self):
        return ["alerts.status"]

    def on_req(self, key: str, payload: Dict[str, Any], ctx: Dict[str, Any]):
        if key == "perf.echo":
            t0 = payload.get("t0")
            return {"ok": True, "t0": t0, "t1": time.time(), "from": ctx["source"]}
        return {"ok": False, "error": f"unknown key {key}"}

    def on_event(self, topic: str, msg):
        print(f"[EchoDaemon] {topic}: {msg.env.payload}")

if __name__ == "__main__":
    PeerB().serve()
