import json, time, uuid, threading
import paho.mqtt.client as mqtt


BASE = "mktl/dev-001"
REQ_TOPIC = f"{BASE}/req"
RESP_TOPIC = f"{BASE}/resp"
TELEMETRY_TOPIC = f"{BASE}/telemetry"
VERBOSE = True

class MqttRpcClient:
    def __init__(self, client_id="libby-bridge-mqtt", host="localhost", port=1883):
        self._c = mqtt.Client(client_id=client_id)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self._host, self._port = host, port
        self._waiters = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()

    def start(self):
        self._c.connect(self._host, self._port, keepalive=30)
        self._c.loop_start()
        self._connected.wait(5)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        client.subscribe(RESP_TOPIC, qos=1)
        client.subscribe(TELEMETRY_TOPIC, qos=0)
        if VERBOSE:
            print(f"[bridge] MQTT connected; sub {RESP_TOPIC}, {TELEMETRY_TOPIC}")
        self._connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            if VERBOSE: print(f"[bridge] MQTT ← {msg.topic}: <non-json>")
            return
        if VERBOSE: print(f"[bridge] MQTT ← {msg.topic}: {data}")
        if msg.topic == RESP_TOPIC:
            msg_id = data.get("msg_id")
            if not msg_id: return
            with self._lock:
                w = self._waiters.get(msg_id)
            if w:
                w["resp"] = data
                w["ev"].set()

    def call(self, action: str, payload=None, timeout=5.0) -> dict:
        msg_id = str(uuid.uuid4())
        req = {"type": "request", "action": action, "payload": payload or {}, "msg_id": msg_id, "ts": time.time()}
        if VERBOSE: print(f"[bridge] MQTT → {REQ_TOPIC}: {req}")
        ev = threading.Event()
        with self._lock:
            self._waiters[msg_id] = {"ev": ev, "resp": None}
        self._c.publish(REQ_TOPIC, json.dumps(req), qos=1)
        if not ev.wait(timeout):
            with self._lock:
                self._waiters.pop(msg_id, None)
            raise TimeoutError(f"MQTT RPC timeout for '{action}'")
        with self._lock:
            resp = self._waiters.pop(msg_id)["resp"]
        return resp or {"ok": False, "error": "empty response"}
