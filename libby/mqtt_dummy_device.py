import json
import time
import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883
BASE = "mktl/dev-001"
REQ_TOPIC = f"{BASE}/req"
RESP_TOPIC = f"{BASE}/resp"
TELEMETRY_TOPIC = f"{BASE}/telemetry"

state = {
    "switch": False,
    "last_ts": time.time(),
}

def make_resp(msg_id: str, ok: bool, payload: dict | None = None, error: str | None = None) -> dict:
    return {
        "type": "response",
        "ok": ok,
        "payload": payload or {},
        "error": error,
        "msg_id": msg_id,
        "ts": time.time(),
    }

def on_connect(client, userdata, flags, rc):
    client.subscribe(REQ_TOPIC, qos=1)
    print("[dummy] online, listening", REQ_TOPIC)

def on_message(client, userdata, msg):
    try:
        req = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return

    if msg.topic != REQ_TOPIC:
        return

    action = req.get("action", "")
    payload = req.get("payload") or {}
    msg_id = req.get("msg_id", "")

    if action in ("device.status.get", "device.switch.get"):
        resp = make_resp(msg_id, True, {"switch": state["switch"], "ts": time.time()})
    elif action == "device.switch.set":
        desired = payload.get("on", None)
        if isinstance(desired, bool):
            state["switch"] = desired
            state["last_ts"] = time.time()
            print(f"[dummy] switch -> {state['switch']}")
            resp = make_resp(msg_id, True, {"switch": state["switch"], "ts": state["last_ts"]})
        else:
            resp = make_resp(msg_id, False, error="payload.on must be boolean")
    else:
        resp = make_resp(msg_id, False, error=f"unknown action '{action}'")

    client.publish(RESP_TOPIC, json.dumps(resp), qos=1, retain=False)

def main():
    c = mqtt.Client(client_id="dummy-dev-001")
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    # periodic status
    def publish_telemetry():
        t = {"switch": state["switch"], "uptime_s": int(time.time() - state["last_ts"]), "ts": time.time()}
        c.publish(TELEMETRY_TOPIC, json.dumps(t), qos=0, retain=False)

    c.loop_start()
    try:
        while True:
            publish_telemetry()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()

if __name__ == "__main__":
    main()
