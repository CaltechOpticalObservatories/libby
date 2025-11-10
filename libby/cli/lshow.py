from __future__ import annotations
import argparse, json, os, random, sys
from typing import Any, Dict, List, Optional

from libby.config import load_config, with_env_overrides
from libby.libby import Libby

def _rand_port(lo: int = 56000, hi: int = 59000) -> int:
    return random.randint(lo, hi)

def make_client(cfg: Dict[str, Any]) -> Libby:
    self_id = cfg.get("self_id") or f"lshow.cli.{os.getpid()}"
    bind = cfg.get("bind") or f"tcp://*:{_rand_port()}"
    return Libby.zmq(
        self_id=self_id,
        bind=bind,
        address_book=cfg.get("address_book", {}),
        keys=[],
        callback=None,
        discover=bool(cfg.get("discovery_enabled", False)), 
        discover_interval_s=float(cfg.get("discovery_interval_s", 3.0)),
        hello_on_start=True,
    )

def request_keywords(lib: Libby, peer: str, timeout_s: float) -> List[str]:
    try:
        try:
            resp = lib.request(peer, "keywords", {}, timeout_s=timeout_s)
        except TypeError:
            resp = lib.request(peer, "keywords", {})
    except Exception as ex:
        raise RuntimeError(f"RPC to {peer!r} failed: {ex}") from ex

    if isinstance(resp, list):
        return [str(x) for x in resp]
    if isinstance(resp, dict) and isinstance(resp.get("keys"), list):
        return [str(x) for x in resp["keys"]]
    raise RuntimeError(f"Unexpected response from {peer!r}: {resp!r}")

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lshow", description="Show the keys of a given peer")
    ap.add_argument("-p", "--peer", required=True, help="Target peer id (e.g., peer-B)")
    ap.add_argument("-c", "--config", help="Optional JSON/YAML client config (e.g., address_book)")
    ap.add_argument("--timeout", type=float, default=5.0, help="RPC timeout seconds (default: 5.0)")
    ap.add_argument("--json", action="store_true", help="Output raw JSON array instead of lines")
    ns = ap.parse_args(argv)

    cfg: Dict[str, Any] = with_env_overrides(load_config(ns.config), "LIBBY_") if ns.config else with_env_overrides({}, "LIBBY_")

    lib = None
    try:
        lib = make_client(cfg)
        keys = request_keywords(lib, ns.peer, ns.timeout)
        if ns.json:
            print(json.dumps(keys, indent=2, sort_keys=True))
        else:
            for k in keys:
                print(k)
        return 0
    except Exception as ex:
        print(f"lshow: {ex}", file=sys.stderr)
        return 2
    finally:
        if lib is not None:
            try: lib.stop()
            except Exception: pass

if __name__ == "__main__":
    raise SystemExit(main())
