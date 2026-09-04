import json
import threading
import time
from typing import Any, Callable, Dict, Optional
import zmq
from bamboo.transport import Transport

DestStr = str
SrcStr = str

# Presence pings ride the same DEALER/ROUTER sockets as protocol traffic;
# _PRESENCE_MARKER distinguishes them from real bamboo frames on receipt
# (a real frame is JSON too, see bamboo/wire.py, but never has this key).
_PRESENCE_MARKER = "_libby_presence"
_PRESENCE_INTERVAL_S = 3.0
_PRESENCE_STALE_S = 10.0

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

    def __init__(self, bind_router: str, address_book: Dict[str, Dict[str, Any]], my_id: str, group_id: Optional[str] = None):
        """
        address_book: peer_id -> {"endpoint": str, "group_id": Optional[str]}.
        `group_id` is corroborated/refreshed by presence pings as peers are
        heard from; find_peer only ever resolves entries already present here
        (see plans/libby_find_peer_design.md) - no zero-conf bootstrap.
        """
        self._ctx = zmq.Context.instance()

        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.LINGER, 0)
        self._router.bind(bind_router)
        self._router_id_by_peer: Dict[str, bytes] = {}

        self._dealers: Dict[str, zmq.Socket] = {}
        self._book: Dict[str, Dict[str, Any]] = {
            peer_id: {"endpoint": entry["endpoint"], "group_id": entry.get("group_id"), "last_seen": None}
            for peer_id, entry in address_book.items()
        }
        self._book_lock = threading.Lock()
        self._cb: Optional[Callable[[SrcStr, bytes], None]] = None

        self._stop = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._presence_thread: Optional[threading.Thread] = None
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
        self._presence_thread = threading.Thread(target=self._presence_loop, daemon=True)
        self._presence_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._presence_thread:
            self._presence_thread.join(timeout=1.0)
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

    def reply_to(self, peer_id: str, frame: bytes) -> bool:
        """
        Try to send directly back to the peer using the ROUTER routing-id
        we observed on the incoming request. Returns True if used, False otherwise.
        """
        rid = self._router_id_by_peer.get(peer_id)
        if rid is None:
            return False
        with self._send_lock:
            self._router.send_multipart([rid, frame])
        return True

    def send(self, dest: DestStr, frame: bytes) -> None:
        """
        dest:
        - "peer:<peer_id>"  or  "<peer_id>"  -> direct to that peer
        - "broadcast:*"                      -> to all known peers
        """
        # 1) broadcast
        if dest.startswith("broadcast:"):
            with self._send_lock:
                for _peer_id, _entry in self._book.items():
                    dealer = self._dealers.get(_peer_id)
                    if dealer is None:
                        dealer = self._new_dealer(_peer_id, _entry["endpoint"])
                        self._dealers[_peer_id] = dealer
                    dealer.send(frame)
            return

        # 2) normalize to plain id
        peer_id = dest.split(":", 1)[1] if dest.startswith("peer:") else dest

        # 3) preferred path: DEALER via address_book
        entry = self._book.get(peer_id)
        if entry:
            dealer = self._dealers.get(peer_id)
            if dealer is None:
                dealer = self._new_dealer(peer_id, entry["endpoint"])
                self._dealers[peer_id] = dealer
            with self._send_lock:
                dealer.send(frame)
            return

        # 4) fallback: reply via ROUTER if we cached this peer's routing-id
        rid_map = getattr(self, "_router_id_by_peer", None)
        if rid_map is not None:
            rid = rid_map.get(peer_id)
            if rid is not None:
                with self._send_lock:
                    self._router.send_multipart([rid, frame])
                return

        # 5) unknown route -> drop silently (matches bamboo no-NACK semantics)
        return


    def add_peer(self, peer_id: str, endpoint: str, group_id: Optional[str] = None) -> None:
        """Dynamically add or update an endpoint for a peer."""
        with self._book_lock:
            existing = self._book.get(peer_id)
            self._book[peer_id] = {
                "endpoint": endpoint,
                "group_id": group_id,
                "last_seen": existing["last_seen"] if existing else None,
            }
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

                self._router_id_by_peer[src_peer] = ident
                if self._handle_if_presence(src_peer, payload):
                    continue
                if self._cb:
                    # Pass the *remote* peer id as the source
                    self._cb(f"peer:{src_peer}", payload)

    def find_peer(self, group_id: Optional[str], peer_id: str, timeout_s: float = 2.0) -> bool:
        """
        Return True if `peer_id` is in the address book with a group_id
        confirmed by a recent presence ping, waiting up to `timeout_s` for one
        to arrive if it hasn't yet. Peers with no address-book entry can never
        be found here - see plans/libby_find_peer_design.md.
        """
        deadline = time.time() + timeout_s
        while True:
            entry = self._book.get(peer_id)
            if entry is not None and entry.get("group_id") == group_id:
                last_seen = entry.get("last_seen")
                if last_seen is not None and time.time() - last_seen < _PRESENCE_STALE_S:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def _handle_if_presence(self, src_peer: str, payload: bytes) -> bool:
        """If `payload` is a presence ping, refresh _book and return True.

        A real bamboo frame is JSON too (see bamboo/wire.py) but never has
        `_PRESENCE_MARKER`, so this can't misclassify protocol traffic.
        """
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict) or not data.get(_PRESENCE_MARKER):
            return False
        with self._book_lock:
            entry = self._book.get(src_peer)
            if entry is not None:
                entry["group_id"] = data.get("group_id")
                entry["last_seen"] = time.time()
        return True

    def _publish_presence(self) -> None:
        """Ping every peer already in the address book with our identity."""
        body = json.dumps({
            _PRESENCE_MARKER: True,
            "group_id": self._group_id,
        }).encode("utf-8")
        with self._send_lock:
            for peer_id, entry in list(self._book.items()):
                dealer = self._dealers.get(peer_id)
                if dealer is None:
                    dealer = self._new_dealer(peer_id, entry["endpoint"])
                    self._dealers[peer_id] = dealer
                try:
                    dealer.send(body)
                except zmq.ZMQError:
                    pass

    def _presence_loop(self) -> None:
        """Periodically ping known peers so their find_peer can see us."""
        self._publish_presence()
        while not self._stop.wait(timeout=_PRESENCE_INTERVAL_S):
            self._publish_presence()
