import time
from libby import Libby
from libby import MqttRpcClient

PEER_ID = "peer-bridge"
BIND = "tcp://*:5557"
ADDRESS_BOOK = {}
EXPOSED_KEYS = ["device.status.get", "device.switch.get", "device.switch.set"]
VERBOSE = True


def make_libby_handler(mqtt_rpc: MqttRpcClient):
    def handle(payload: dict, meta: dict) -> dict:
        key = meta.get("key", "")
        ttl_ms = meta.get("ttl_ms", 5000)

        if VERBOSE:
            print(f"[bridge] Libby ← key={key} payload={payload} meta={{src:{meta.get('src')}, dst:{meta.get('dst')}, ttl_ms:{ttl_ms}}}")

        if key not in EXPOSED_KEYS:
            return {"error": f"unknown key '{key}'"}

        try:
            resp = mqtt_rpc.call(key, payload or {}, timeout=ttl_ms / 1000.0)
        except TimeoutError as e:
            if VERBOSE:
                print(f"[bridge] Libby → timeout for {key}: {e}")
            return {"error": str(e)}

        if not isinstance(resp, dict):
            return {"error": "malformed device response"}

        if not resp.get("ok", False):
            err = resp.get("error", "device error")
            if VERBOSE:
                print(f"[bridge] Libby → device error for {key}: {err}")
            return {"error": err}

        out = resp.get("payload", {})
        if VERBOSE:
            print(f"[bridge] Libby → payload for {key}: {out}")
        return out
    return handle

def main():
    mqtt_rpc = MqttRpcClient()
    mqtt_rpc.start()
    with Libby.zmq(
        self_id=PEER_ID,
        bind=BIND,
        address_book=ADDRESS_BOOK,
        keys=EXPOSED_KEYS,                    # advertise keys the bridge serves
        callback=make_libby_handler(mqtt_rpc),
        discover=True,
        discover_interval_s=1.5,
        hello_on_start=True,
    ) as libby:
        print(f"[{PEER_ID}] Libby↔MQTT bridge online. Serving: {', '.join(EXPOSED_KEYS)}")
        while True:
            time.sleep(1)

if __name__ == "__main__":
    main()
