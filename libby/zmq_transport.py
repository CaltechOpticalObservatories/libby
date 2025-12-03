import threading
from typing import Callable, Dict, Optional
import zmq
from bamboo.transport import Transport

DestStr = str
SrcStr = str

class ZmqTransport(Transport):
    """
    Simple ROUTER (bind) + per-peer DEALER (connect) transport.

    - This peer binds a ROUTER at `bind_router`.
    - For each remote peer_id in `address_book`, we lazily create a DEALER
      and set its ZMQ.IDENTITY to *peer_id* so the remote can see who sent.
    - Incoming frames arrive on ROUTER as:
         [IDENT, PAYLOAD]  or  [IDENT, b"", PAYLOAD]
      We pass IDENT as "peer:<peer_id>" to the Protocol callback.
    """

    def __init__(self, bind_router: str, address_book: Dict[str, str], my_id: str, group_id: Optional[str] = None):
        self._ctx = zmq.Context.instance()

        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.LINGER, 0)
        self._router.bind(bind_router)

        self._dealers: Dict[str, zmq.Socket] = {}
        self._book: Dict[str, str] = dict(address_book)
        self._cb: Optional[Callable[[SrcStr, bytes], None]] = None

        self._stop = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._poller = zmq.Poller()
        self._poller.register(self._router, zmq.POLLIN)
        self._send_lock = threading.Lock()

        self._id = my_id  # local peer id
        self._group_id = group_id

    @property
    def group_id(self) -> Optional[str]:
        return self._group_id

    @property
    def mtu(self) -> int:
        # default size
        return 512 * 1024

    def start(self) -> None:
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        # Close dealers
        for s in list(self._dealers.values()):
            try:
                s.close(0)
            except Exception:
                pass
        self._dealers.clear()
        # Close router last
        try:
            self._poller.unregister(self._router)
        except Exception:
            pass
        try:
            self._router.close(0)
        except Exception:
            pass
        # Do NOT terminate the shared ZMQ context (instance())

    def on_receive(self, cb: Callable[[SrcStr, bytes], None]) -> None:
        self._cb = cb

    def send(self, dest: DestStr, frame: bytes) -> None:
        """
        dest:
          - "peer:<peer_id>"     -> send direct to that peer via DEALER
          - "broadcast:*"        -> send to all known peers (fire-and-forget)
        """
        if dest.startswith("peer:"):
            peer_id = dest.split(":", 1)[1]
            endpoint = self._book.get(peer_id)
            if not endpoint:
                return  # unknown peer_id; drop silently to match bamboo no-NACK
            dealer = self._dealers.get(peer_id)
            if dealer is None:
                dealer = self._new_dealer(peer_id, endpoint)
                self._dealers[peer_id] = dealer
            with self._send_lock:
                dealer.send(frame)
            return

        if dest.startswith("broadcast:"):
            with self._send_lock:
                for peer_id, endpoint in self._book.items():
                    dealer = self._dealers.get(peer_id)
                    if dealer is None:
                        dealer = self._new_dealer(peer_id, endpoint)
                        self._dealers[peer_id] = dealer
                    dealer.send(frame)

    def add_peer(self, peer_id: str, endpoint: str) -> None:
        """Dynamically add or update an endpoint for a peer."""
        self._book[peer_id] = endpoint
        # Dealer will be (re)created lazily on first send

    def _new_dealer(self, peer_id: str, endpoint: str) -> zmq.Socket:
        """
        Create a DEALER socket that *identifies as this local peer* to the remote ROUTER.

        NOTE: For ROUTER to see the sender as <peer_id>, the *sender’s* identity
              must be set to that peer’s own id. Here we set identity to self._id,
              because we (this process) are the sender.
        """
        s = self._ctx.socket(zmq.DEALER)
        s.setsockopt(zmq.LINGER, 0)
        # This socket represents *us* when talking to <peer_id>’s ROUTER.
        # So identity must be our local id.
        s.setsockopt(zmq.IDENTITY, self._id.encode("utf-8"))
        s.connect(endpoint)
        return s

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                socks = dict(self._poller.poll(100))
            except zmq.ZMQError:
                break

            if socks.get(self._router) == zmq.POLLIN:
                try:
                    parts = self._router.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue
                if not parts:
                    continue

                #   [IDENT, PAYLOAD]
                #   [IDENT, b"", PAYLOAD]
                ident = parts[0]
                payload = parts[-1]
                try:
                    src_peer = ident.decode("utf-8", errors="ignore")
                except Exception:
                    src_peer = "unknown"

                if self._cb:
                    # Pass the *remote* peer id as the source
                    self._cb(f"peer:{src_peer}", payload)
