import threading
from typing import Callable, Dict
import zmq
from bamboo.transport import Transport

DestStr = str
SrcStr = str

class ZmqTransport(Transport):

    def __init__(self, bind_router: str, address_book: Dict[str, str], my_id: str):
        self._ctx = zmq.Context.instance()
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.bind(bind_router)

        self._dealers: Dict[str, zmq.Socket] = {}
        self._book = dict(address_book)
        self._cb: Callable[[SrcStr, bytes], None] | None = None

        self._stop = threading.Event()
        self._rx_thread: threading.Thread | None = None
        self._poller = zmq.Poller()
        self._poller.register(self._router, zmq.POLLIN)
        self._send_lock = threading.Lock()

        self._id = my_id

    @property
    def mtu(self) -> int: return 512 * 1024

    def start(self) -> None:
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._rx_thread: self._rx_thread.join(timeout=1.0)
        for s in self._dealers.values():
            try: s.close(0)
            except Exception: pass
        try: self._router.close(0)
        except Exception: pass

    def on_receive(self, cb: Callable[[SrcStr, bytes], None]) -> None:
        self._cb = cb

    def send(self, dest: DestStr, frame: bytes) -> None:
        if dest.startswith("peer:"):
            peer_id = dest.split(":", 1)[1]
            endpoint = self._book.get(peer_id)
            if not endpoint: return
            dealer = self._dealers.get(peer_id)
            if dealer is None:
                dealer = self._ctx.socket(zmq.DEALER)
                dealer.connect(endpoint)
                self._dealers[peer_id] = dealer
            with self._send_lock:
                dealer.send(frame)
            return
        if dest.startswith("broadcast:"):
            with self._send_lock:
                for peer_id, endpoint in self._book.items():
                    dealer = self._dealers.get(peer_id)
                    if dealer is None:
                        dealer = self._ctx.socket(zmq.DEALER)
                        dealer.connect(endpoint)
                        self._dealers[peer_id] = dealer
                    dealer.send(frame)

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                socks = dict(self._poller.poll(100))
            except zmq.ZMQError:
                break
            if self._router in socks and socks[self._router] == zmq.POLLIN:
                try:
                    parts = self._router.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue
                payload = parts[-1]  # [ident, "", payload] -> last is payload
                if self._cb:
                    self._cb(f"peer:{self._id}", payload)
