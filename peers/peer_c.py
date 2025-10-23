from __future__ import annotations
import time
from typing import Dict, Any, Callable
from libby.daemon import LibbyDaemon

class PeerC(LibbyDaemon):
    """
    Minimal but capable:
      - Serves 3 RPC keys, each mapped to its own function:
          clientC.info     -> _rpc_info
          math.add         -> _rpc_math_add
          perf.echo.proxy  -> _rpc_echo_proxy  (forwards to peer-B)
      - Subscribes to one topic: alerts.status
      - On start: waits briefly for peer-B's 'perf.echo', then runs one proxy call and emits one status.
    """

    def config_peer_id(self) -> str: return "peer-C"
    def config_bind(self) -> str:    return "tcp://*:5557"
    def config_address_book(self) -> Dict[str, str]:
        return {"peer-A": "tcp://127.0.0.1:5555", "peer-B": "tcp://127.0.0.1:5556"}
    def config_discovery_enabled(self) -> bool: return True
    def config_discovery_interval_s(self) -> float: return 2.0
    def config_subscriptions(self): return ["alerts.status"]

    def config_rpc_keys(self):
        return list(self._rpc_handlers().keys())

    def _rpc_handlers(self) -> Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]]:
        return {
            "clientC.info":    self._rpc_info,
            "math.add":        self._rpc_math_add,
            "perf.echo.proxy": self._rpc_echo_proxy,
        }

    def on_req(self, key: str, payload: Dict[str, Any], ctx: Dict[str, Any]):
        fn = self._rpc_handlers().get(key)
        if not fn:
            return {"ok": False, "error": f"unknown key '{key}'"}
        try:
            out = fn(payload, ctx)
            return out if ("ok" in out or "error" in out) else {"ok": True, "data": out}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    def _rpc_info(self, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "peer": self.config_peer_id(),
            "time": time.time(),
            "subs": self.config_subscriptions(),
            "keys": self.config_rpc_keys(),
            "from": ctx.get("source"),
        }

    def _rpc_math_add(self, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        a, b = payload.get("a"), payload.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return {"ok": False, "error": "payload must include numeric 'a' and 'b'"}
        return {"ok": True, "sum": a + b}

    def _rpc_echo_proxy(self, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Forward a simple echo request to peer-B's 'perf.echo' key.
        No retries beyond request timeout.
        """
        dest = payload.get("peer", "peer-B")
        res = self.libby.rpc(dest, "perf.echo", {"t0": time.time()}, ttl_ms=6000)
        return {"ok": True, "forwarded_to": dest, "result": res}

    def on_event(self, topic: str, msg):
        if topic == "alerts.status":
            print(f"[peer-C] alerts.status -> {msg.env.payload}")

    def on_start(self, libby):
        try:
            if not libby.wait_for_key("peer-B", "perf.echo", timeout_s=3.0):
                libby.learn_peer_keys("peer-B", ["perf.echo"])
        except AttributeError:
            pass

        # Demo RPCs
        print("[peer-C] math.add(2, 5) ->",
              libby.rpc(self.config_peer_id(), "math.add", {"a": 2, "b": 5}, ttl_ms=2000))

        print("[peer-C] perf.echo.proxy ->",
              libby.rpc(self.config_peer_id(), "perf.echo.proxy", {}, ttl_ms=7000))

        # Emit one status event
        sent = libby.emit("alerts.status", {"source": "peer-C", "ok": True})
        print(f"[peer-C] alerts.status emitted (direct={sent})")

if __name__ == "__main__":
    PeerC().serve()
