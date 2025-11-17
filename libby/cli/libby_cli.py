from __future__ import annotations

import argparse, json, os, signal, sys, time
from typing import Any, Dict, List, Optional
from libby.libby import Libby

DEFAULT_SELF_ID = "cli"
DEFAULT_BIND = "tcp://127.0.0.1:56001"

def _parse_addr_kv(kv: str) -> tuple[str, str]:
    if "=" not in kv:
        raise argparse.ArgumentTypeError("Expected 'peerId=tcp://host:port'")
    k, v = kv.split("=", 1)
    k, v = k.strip(), v.strip()
    if not k or not v:
        raise argparse.ArgumentTypeError("Expected 'peerId=tcp://host:port'")
    return k, v

def _load_book(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("--book JSON must be an object mapping peer->endpoint")
    return {str(k): str(v) for k, v in data.items()}

def _parse_json(s: Optional[str]) -> Dict[str, Any]:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception as ex:
        raise SystemExit(f"--data must be JSON: {ex}")

def _self_id(ns: argparse.Namespace) -> str:
    return ns.self_id or os.environ.get("LIBBY_SELF_ID", DEFAULT_SELF_ID)

def _bind(ns: argparse.Namespace) -> str:
    return ns.bind or os.environ.get("LIBBY_BIND", DEFAULT_BIND)

def _mk_libby(self_id: str, bind: str, book: Dict[str, str]) -> Libby:
    lib = Libby.zmq(
        self_id=self_id,
        bind=bind,
        address_book=book,
        keys=[],
        callback=None,
        discover=True,
        discover_interval_s=2.0,
        hello_on_start=True,
    )
    try:
        lib.hello()
    except Exception:
        pass
    return lib

def cmd_req(ns: argparse.Namespace) -> int:
    book = _load_book(ns.book)
    for kv in ns.addr or []:
        k, v = _parse_addr_kv(kv); book[k] = v

    self_id = _self_id(ns)
    bind = _bind(ns)
    payload = _parse_json(ns.data)

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(self_id, bind, book)
        ttl_ms = int(ns.ttl_ms) if ns.ttl_ms is not None else int(ns.timeout * 1000.0)
        res = lib.rpc(ns.peer, ns.key, payload, ttl_ms=ttl_ms)
        print(json.dumps(res, indent=2 if ns.raw_json else 2))
        return 0 if res.get("status") == "delivered" else 2
    except KeyboardInterrupt:
        return 130
    except Exception as ex:
        print(f"libby-cli req: {ex}", file=sys.stderr)
        return 2
    finally:
        if lib:
            try: lib.stop()
            except Exception: pass

def cmd_sub(ns: argparse.Namespace) -> int:
    topics: List[str] = ns.topics
    if not topics:
        print("sub: provide at least one topic", file=sys.stderr)
        return 2

    book = _load_book(ns.book)
    for kv in ns.addr or []:
        k, v = _parse_addr_kv(kv); book[k] = v

    self_id = _self_id(ns)
    bind = _bind(ns)

    lib: Optional[Libby] = None
    stop = False

    def on_sig(_s, _f):
        nonlocal stop; stop = True

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        lib = _mk_libby(self_id, bind, book)

        def _printer(msg):
            try:
                print(json.dumps(
                    {"source": msg.env.sourceid, "topic": msg.env.key, "payload": msg.env.payload},
                    indent=2 if ns.raw_json else 2,
                ))
            except Exception as ex:
                print(f"[event decode error] {ex}", file=sys.stderr)

        for t in topics:
            lib.listen(t, _printer)
        lib.subscribe(*topics)

        print(f"[libby sub] up: id={self_id} bind={bind} topics={topics}")
        while not stop:
            time.sleep(0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as ex:
        print(f"libby-cli sub: {ex}", file=sys.stderr)
        return 2
    finally:
        if lib:
            try: lib.stop()
            except Exception: pass
        print("[libby sub] stopped")

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="libby-cli",
        description="Simple Libby CLI: request a key or subscribe to topics."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--self-id", help=f"Local peer id (default: {DEFAULT_SELF_ID} or $LIBBY_SELF_ID)")
        p.add_argument("--bind", help=f"Local ROUTER bind (default: {DEFAULT_BIND} or $LIBBY_BIND)")
        p.add_argument("--book", help="Path to JSON {peer_id:'tcp://host:port'}")
        p.add_argument("--addr", action="append", metavar="peer=tcp://host:port",
                       help="Add/override address-book entry (repeatable)")
        p.add_argument("--raw-json", action="store_true", help="Pretty-print JSON")

    pr = sub.add_parser("req", help="Send a keyed request (RPC) to a peer and print the response")
    common(pr)
    pr.add_argument("-p", "--peer", required=True, help="Destination peer id")
    pr.add_argument("-k", "--key", required=True, help="Key to request (service name)")
    pr.add_argument("-d", "--data", help="JSON payload to send (default: {})")
    pr.add_argument("--timeout", type=float, default=8.0, help="Timeout seconds (default 8.0)")
    pr.add_argument("--ttl-ms", type=int, help="Override TTL ms (default: timeout*1000)")
    pr.set_defaults(func=cmd_req)

    ps = sub.add_parser("sub", help="Subscribe to one or more topics and print publishes")
    common(ps)
    ps.add_argument("topics", nargs="+", help="Topic(s) to subscribe to")
    ps.set_defaults(func=cmd_sub)

    return ap

def main(argv: Optional[List[str]] = None) -> int:
    ns = build_parser().parse_args(argv)
    return ns.func(ns)

if __name__ == "__main__":
    raise SystemExit(main())
