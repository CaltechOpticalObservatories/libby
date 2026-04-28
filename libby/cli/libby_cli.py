"""Libby CLI — show/modify keywords on libby peers, plus raw req/sub."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from libby.libby import Libby

DEFAULT_SELF_ID = "cli"
DEFAULT_BIND = "tcp://127.0.0.1:56001"
DEFAULT_RABBITMQ_URL = "amqp://localhost"
DEFAULT_TRANSPORT = "rabbitmq"
DEFAULT_CONFIG_PATH = Path.home() / ".libby" / "cli_config.yaml"
DEFAULT_TIMEOUT_S = 3.0
MOTION_TIMEOUT_S = 30.0


def _load_config(path: Optional[str]) -> Dict[str, Any]:
    """Load cli_config.yaml. Missing file → empty dict."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as ex:
        raise SystemExit(f"libby: failed to load {p}: {ex}")
    if not isinstance(data, dict):
        raise SystemExit(f"libby: {p} must parse to a dict, got {type(data).__name__}")
    return data


def _resolve_transport(namespace: argparse.Namespace, config: Dict[str, Any]) -> str:
    transport = namespace.transport or config.get("transport") or DEFAULT_TRANSPORT
    if transport not in ("zmq", "rabbitmq"):
        raise SystemExit(f"libby: invalid transport: {transport}")
    return transport


def _resolve_rabbitmq_url(namespace: argparse.Namespace, config: Dict[str, Any]) -> str:
    return namespace.rabbitmq_url or config.get("rabbitmq_url") or DEFAULT_RABBITMQ_URL


def _resolve_address_book(namespace: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, str]:
    book: Dict[str, str] = dict(config.get("peers") or {})
    for kv in (namespace.addr or []):
        peer, address = _parse_addr_kv(kv)
        book[peer] = address
    return book


def _parse_addr_kv(kv: str) -> Tuple[str, str]:
    if "=" not in kv:
        raise argparse.ArgumentTypeError("Expected 'peer_id=tcp://host:port'")
    peer, address = kv.split("=", 1)
    peer, address = peer.strip(), address.strip()
    if not peer or not address:
        raise argparse.ArgumentTypeError("Expected 'peer_id=tcp://host:port'")
    return peer, address


def _mk_libby(namespace: argparse.Namespace, config: Dict[str, Any]) -> Libby:
    transport = _resolve_transport(namespace, config)
    self_id = namespace.self_id or DEFAULT_SELF_ID
    if transport == "rabbitmq":
        return Libby.rabbitmq(
            self_id=self_id,
            rabbitmq_url=_resolve_rabbitmq_url(namespace, config),
            keys=[],
        )
    return Libby.zmq(
        self_id=self_id,
        bind=namespace.bind or DEFAULT_BIND,
        address_book=_resolve_address_book(namespace, config),
        keys=[],
        callback=None,
        discover=True,
        discover_interval_s=2.0,
        hello_on_start=True,
    )


def _parse_keyword(arg: str, *, allow_pattern: bool = False) -> Tuple[str, str, str]:
    """Parse '<group>.<scope>.<name>' into (group, scope, name).

    With ``allow_pattern=True``, ``%`` is allowed in the name segment.
    Group and scope must always be explicit.
    """
    parts = arg.split(".", 2)
    if len(parts) < 3 or not all(parts):
        raise SystemExit(f"libby: keyword must be <group>.<scope>.<name>, got: {arg}")
    group, scope, name = parts
    if "%" in group or "%" in scope:
        raise SystemExit(
            f"libby: wildcards (%) are not allowed in <group> or <scope>: {arg}"
        )
    if "%" in name and not allow_pattern:
        raise SystemExit(
            f"libby: this verb requires an exact keyword name (no %): {arg}"
        )
    return group, scope, name


def _peer_id(group: str, scope: str) -> str:
    return f"{group}_{scope}"


def _coerce_value(value: str) -> Any:
    """Coerce a modify value string.

    Empty / 'null' → None; 'true'/'false' → bool; parseable as int → int;
    parseable as float → float; otherwise the original string.
    """
    if value == "":
        return None
    low = value.strip().lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _emit_one(qualified: str, resp: Dict[str, Any], *, as_json: bool) -> int:
    """Print one keyword response. Return exit code (0 ok, 2 error)."""
    if not isinstance(resp, dict):
        return _emit_error(qualified, f"unexpected response: {resp}", as_json=as_json)
    if as_json:
        out = {"qualified": qualified, **resp}
        print(json.dumps(out, indent=2))
        return 0 if resp.get("ok") else 2
    if not resp.get("ok"):
        print(f"libby: {qualified}: {resp.get('error', 'unknown error')}", file=sys.stderr)
        return 2
    value = resp.get("value")
    units = resp.get("units")
    if units:
        print(f"{qualified} = {value} {units}")
    else:
        print(f"{qualified} = {value}")
    return 0


def _emit_many(rows: List[Tuple[str, Dict[str, Any]]], *, as_json: bool) -> int:
    """Print many keyword responses (show wildcard). Return exit code."""
    if as_json:
        out = [{"qualified": qualified, **resp} for qualified, resp in rows]
        print(json.dumps(out, indent=2))
        return 0 if all(r.get("ok") for _, r in rows) else 2
    width = max(len(qualified) for qualified, _ in rows)
    rc = 0
    for qualified, resp in rows:
        if not resp.get("ok"):
            print(f"{qualified:<{width}}  <error: {resp.get('error', 'unknown')}>")
            rc = 2
            continue
        value = resp.get("value")
        units = resp.get("units")
        if units:
            print(f"{qualified:<{width}} = {value} {units}")
        else:
            print(f"{qualified:<{width}} = {value}")
    return rc


def _emit_list(qualified_names: List[str], *, as_json: bool) -> int:
    """Print a list of qualified keyword names."""
    if as_json:
        print(json.dumps(qualified_names, indent=2))
    else:
        for name in qualified_names:
            print(name)
    return 0


def _emit_error(qualified: Optional[str], message: str, *, as_json: bool) -> int:
    """Emit an error. JSON: object on stdout. Text: 'libby: ...' on stderr."""
    if as_json:
        out: Dict[str, Any] = {"ok": False, "error": message}
        if qualified:
            out["qualified"] = qualified
        print(json.dumps(out, indent=2))
    else:
        prefix = f"libby: {qualified}: " if qualified else "libby: "
        print(f"{prefix}{message}", file=sys.stderr)
    return 2


def _emit_describe(qualified: str, resp: Dict[str, Any], *, as_json: bool) -> int:
    """Render keys.describe output."""
    if as_json:
        out = {"qualified": qualified, **resp}
        print(json.dumps(out, indent=2))
        return 0
    fields = [
        ("type",        resp.get("type")),
        ("readonly",    resp.get("readonly")),
        ("writeonly",   resp.get("writeonly")),
        ("nullable",    resp.get("nullable")),
        ("units",       resp.get("units")),
        ("description", resp.get("description")),
    ]
    visible = [(k, v) for k, v in fields if v is not None and v != ""]
    print(f"{qualified}:")
    if not visible:
        return 0
    width = max(len(k) for k, _ in visible)
    for key, value in visible:
        print(f"  {key:<{width}}  {value}")
    return 0


def _default_timeout(verb: str, name: str) -> float:
    """Bump timeout for motion modifies (positionvalue/positionnamed)."""
    if verb == "modify" and (
        name.startswith("positionvalue") or name.startswith("positionnamed")
    ):
        return MOTION_TIMEOUT_S
    return DEFAULT_TIMEOUT_S


def _rpc_show_one(lib: Libby, peer: str, name: str, timeout: float) -> Dict[str, Any]:
    result = lib.rpc(peer, name, {}, ttl_ms=int(timeout * 1000))
    return result.get("resp", result) if isinstance(result, dict) else {}


def _rpc_keys_list(lib: Libby, peer: str, pattern: str, timeout: float) -> Dict[str, Any]:
    result = lib.rpc(peer, "keys.list", {"pattern": pattern}, ttl_ms=int(timeout * 1000))
    return result.get("resp", result) if isinstance(result, dict) else {}


def _rpc_keys_describe(lib: Libby, peer: str, name: str, timeout: float) -> Dict[str, Any]:
    result = lib.rpc(peer, "keys.describe", {"name": name}, ttl_ms=int(timeout * 1000))
    return result.get("resp", result) if isinstance(result, dict) else {}


def cmd_show(namespace: argparse.Namespace) -> int:
    config = _load_config(namespace.config)
    group, scope, name = _parse_keyword(namespace.keyword, allow_pattern=True)
    peer = _peer_id(group, scope)
    timeout = (
        namespace.timeout
        if namespace.timeout is not None
        else _default_timeout("show", name)
    )
    qualified_arg = f"{group}.{scope}.{name}"

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        if "%" in name:
            list_resp = _rpc_keys_list(lib, peer, name, timeout)
            if not list_resp.get("ok"):
                return _emit_error(
                    qualified_arg,
                    list_resp.get("error", "unknown error"),
                    as_json=namespace.json,
                )
            matches: List[str] = list_resp.get("matches", [])
            if not matches:
                return 3
            rows: List[Tuple[str, Dict[str, Any]]] = [
                (f"{group}.{scope}.{m}", _rpc_show_one(lib, peer, m, timeout))
                for m in matches
            ]
            return _emit_many(rows, as_json=namespace.json)
        return _emit_one(
            qualified_arg,
            _rpc_show_one(lib, peer, name, timeout),
            as_json=namespace.json,
        )
    except Exception as ex:
        return _emit_error(qualified_arg, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_list(namespace: argparse.Namespace) -> int:
    config = _load_config(namespace.config)
    group, scope, pattern = _parse_keyword(namespace.pattern, allow_pattern=True)
    peer = _peer_id(group, scope)
    timeout = namespace.timeout if namespace.timeout is not None else DEFAULT_TIMEOUT_S

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        resp = _rpc_keys_list(lib, peer, pattern, timeout)
        if not resp.get("ok"):
            return _emit_error(
                namespace.pattern,
                resp.get("error", "unknown error"),
                as_json=namespace.json,
            )
        matches: List[str] = resp.get("matches", [])
        if not matches:
            if namespace.json:
                print(json.dumps([], indent=2))
            return 3
        qualified_names = [f"{group}.{scope}.{m}" for m in matches]
        return _emit_list(qualified_names, as_json=namespace.json)
    except Exception as ex:
        return _emit_error(namespace.pattern, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_describe(namespace: argparse.Namespace) -> int:
    config = _load_config(namespace.config)
    group, scope, name = _parse_keyword(namespace.keyword, allow_pattern=False)
    peer = _peer_id(group, scope)
    qualified = f"{group}.{scope}.{name}"
    timeout = namespace.timeout if namespace.timeout is not None else DEFAULT_TIMEOUT_S

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        resp = _rpc_keys_describe(lib, peer, name, timeout)
        if not resp.get("ok"):
            return _emit_error(
                qualified,
                resp.get("error", "unknown error"),
                as_json=namespace.json,
            )
        return _emit_describe(qualified, resp, as_json=namespace.json)
    except Exception as ex:
        return _emit_error(qualified, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_modify(namespace: argparse.Namespace) -> int:
    config = _load_config(namespace.config)

    # Two forms accepted:
    #   modify <group>.<scope>.<name>=<value>
    #   modify <group>.<scope>.<name> <value>
    if namespace.value is None:
        if "=" not in namespace.keyword:
            return _emit_error(
                None,
                "modify: provide <keyword>=<value> or <keyword> <value>",
                as_json=namespace.json,
            )
        keyword_str, value_str = namespace.keyword.split("=", 1)
    else:
        keyword_str = namespace.keyword
        value_str = namespace.value

    group, scope, name = _parse_keyword(keyword_str)
    peer = _peer_id(group, scope)
    qualified = f"{group}.{scope}.{name}"
    timeout = (
        namespace.timeout
        if namespace.timeout is not None
        else _default_timeout("modify", name)
    )
    value = _coerce_value(value_str)

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        result = lib.rpc(peer, name, {"value": value}, ttl_ms=int(timeout * 1000))
        resp = result.get("resp", result) if isinstance(result, dict) else {}
        return _emit_one(qualified, resp, as_json=namespace.json)
    except Exception as ex:
        return _emit_error(qualified, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def _parse_json(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception as ex:
        raise SystemExit(f"--data must be JSON: {ex}")


def cmd_req(namespace: argparse.Namespace) -> int:
    """Raw RPC for debugging — works on either transport."""
    config = _load_config(namespace.config)
    payload = _parse_json(namespace.data)
    timeout = namespace.timeout if namespace.timeout is not None else DEFAULT_TIMEOUT_S

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        ttl_ms = int(namespace.ttl_ms) if namespace.ttl_ms is not None else int(timeout * 1000)
        result = lib.rpc(namespace.peer, namespace.key, payload, ttl_ms=ttl_ms)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict) and result.get("status") == "delivered":
            return 0
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as ex:
        print(f"libby req: {ex}", file=sys.stderr)
        return 2
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_sub(namespace: argparse.Namespace) -> int:
    """Subscribe to topics. ZMQ-only."""
    config = _load_config(namespace.config)
    if _resolve_transport(namespace, config) != "zmq":
        print("libby sub: only ZMQ transport is supported", file=sys.stderr)
        return 2
    if not namespace.topics:
        print("libby sub: provide at least one topic", file=sys.stderr)
        return 2

    stop_flag = False

    def on_signal(_s, _f):
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)

        def _printer(msg):
            try:
                print(json.dumps({
                    "source":  msg.env.sourceid,
                    "topic":   msg.env.key,
                    "payload": msg.env.payload,
                }, indent=2))
            except Exception as ex:
                print(f"[event decode error] {ex}", file=sys.stderr)

        for topic in namespace.topics:
            lib.listen(topic, _printer)
        lib.subscribe(*namespace.topics)

        print(f"[libby sub] up: id={namespace.self_id or DEFAULT_SELF_ID} topics={namespace.topics}")
        while not stop_flag:
            time.sleep(0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as ex:
        print(f"libby sub: {ex}", file=sys.stderr)
        return 2
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass
        print("[libby sub] stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libby",
        description="Libby CLI: show/modify keywords on libby peers.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--config",
                       help=f"Path to cli_config.yaml (default: {DEFAULT_CONFIG_PATH})")
        p.add_argument("--transport", choices=("zmq", "rabbitmq"),
                       help="Override transport (default: from config or 'rabbitmq')")
        p.add_argument("--rabbitmq-url",
                       help=f"Override RabbitMQ URL (default: from config or '{DEFAULT_RABBITMQ_URL}')")
        p.add_argument("--bind",
                       help=f"ZMQ ROUTER bind (default: {DEFAULT_BIND})")
        p.add_argument("--addr", action="append", metavar="peer=tcp://host:port",
                       help="ZMQ address-book entry; repeatable")
        p.add_argument("--self-id",
                       help=f"Local peer id (default: {DEFAULT_SELF_ID})")
        p.add_argument("--timeout", type=float,
                       help=f"Timeout seconds (default: {DEFAULT_TIMEOUT_S}; "
                            f"{MOTION_TIMEOUT_S} for motion modifies)")
        p.add_argument("--json", action="store_true",
                       help="Emit JSON to stdout instead of the pretty text format")

    p_show = sub.add_parser("show", help="Read a keyword's value (% allowed in name)")
    add_common(p_show)
    p_show.add_argument("keyword",
                        help="<group>.<scope>.<name> (% allowed in name segment)")
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="List keyword names matching a pattern")
    add_common(p_list)
    p_list.add_argument("pattern",
                        help="<group>.<scope>.<name-pattern> (% wildcard in name)")
    p_list.set_defaults(func=cmd_list)

    p_describe = sub.add_parser("describe", help="Show metadata for a keyword")
    add_common(p_describe)
    p_describe.add_argument("keyword",
                            help="<group>.<scope>.<name> (exact, no wildcards)")
    p_describe.set_defaults(func=cmd_describe)

    p_modify = sub.add_parser("modify", help="Set a keyword's value")
    add_common(p_modify)
    p_modify.add_argument(
        "keyword",
        help="<group>.<scope>.<name>=<value> or <group>.<scope>.<name> (+ value arg)",
    )
    p_modify.add_argument("value", nargs="?",
                          help="Value (if not using = form)")
    p_modify.set_defaults(func=cmd_modify)

    p_req = sub.add_parser("req", help="Raw RPC: send a keyed request and print the response")
    add_common(p_req)
    p_req.add_argument("-p", "--peer", required=True, help="Destination peer id")
    p_req.add_argument("-k", "--key", required=True, help="Key to request (service name)")
    p_req.add_argument("-d", "--data", help="JSON payload to send (default: {})")
    p_req.add_argument("--ttl-ms", type=int, help="Override TTL ms (default: timeout*1000)")
    p_req.set_defaults(func=cmd_req)

    p_sub = sub.add_parser("sub", help="Subscribe to topic(s) and print publishes (ZMQ only)")
    add_common(p_sub)
    p_sub.add_argument("topics", nargs="+", help="Topic(s) to subscribe to")
    p_sub.set_defaults(func=cmd_sub)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    namespace = build_parser().parse_args(argv)
    return namespace.func(namespace)


if __name__ == "__main__":
    raise SystemExit(main())
