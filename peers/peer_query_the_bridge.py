from libby import Libby

PEER_ID = "peer-controller"
BIND = "tcp://*:5558"
ADDRESS_BOOK = {
    "peer-bridge": "tcp://127.0.0.1:5557",
}
BRIDGE = "peer-bridge"

def main():
    with Libby.zmq(
        self_id=PEER_ID,
        bind=BIND,
        address_book=ADDRESS_BOOK,
        discover=True,
        discover_interval_s=1.5,
        hello_on_start=True,
    ) as libby:
        libby.learn_peer_keys(BRIDGE, [
            "device.status.get",
            "device.switch.get",
            "device.switch.set",
        ])

        print("[ctl] → device.status.get")
        r = libby.request(BRIDGE, key="device.status.get", payload={}, ttl_ms=8000)
        print("[ctl] ←", r)

        print("[ctl] → device.switch.set(on=True)")
        r = libby.request(BRIDGE, key="device.switch.set", payload={"on": True}, ttl_ms=8000)
        print("[ctl] ←", r)

        print("[ctl] → device.switch.get")
        r = libby.request(BRIDGE, key="device.switch.get", payload={}, ttl_ms=8000)
        print("[ctl] ←", r)

        print("[ctl] → device.switch.set(on=False)")
        r = libby.request(BRIDGE, key="device.switch.set", payload={"on": False}, ttl_ms=8000)
        print("[ctl] ←", r)

if __name__ == "__main__":
    main()
