"""Libby CLI — show/modify keywords on libby peers, plus raw req/sub."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from libby.config_resolve import (
    DEFAULT_BIND,
    DEFAULT_CONFIG_PATH,
    DEFAULT_RABBITMQ_URL,
    load_cli_config,
    resolve_address_book,
    resolve_rabbitmq_url,
    resolve_transport,
)
from libby.errors import KeywordError, LibbyError
from libby.libby import Libby
from libby.naming import coerce_value, parse_keyword, peer_id
from libby.response import unwrap

DEFAULT_SELF_ID = "cli"
DEFAULT_TIMEOUT_S = 3.0

DEFAULT_LOG_FILE = Path.home() / ".libby" / "cli.log"
DEFAULT_LOG_LEVEL = "WARNING"

# Triage log — separate from stdout/stderr, which remain reserved for
# command output (including machine-parseable --json). Off by default
# beyond WARNING so normal use stays quiet; --log-level/-v turn it up.
logger = logging.getLogger("libby.cli")


def _redact_url(url: Optional[str]) -> Optional[str]:
    """Mask userinfo (e.g. amqp://user:pass@host) before it hits the log."""
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        if not (parsed.username or parsed.password):
            return url
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(parsed._replace(netloc=f"***:***@{netloc}"))
    except Exception:
        return "***"


def _redact_argv(argv: List[str]) -> List[str]:
    """Mask ``--rabbitmq-url``'s value before argv is logged verbatim."""
    out: List[str] = []
    take_next = False
    for tok in argv:
        if take_next:
            out.append(_redact_url(tok))
            take_next = False
        elif tok == "--rabbitmq-url":
            out.append(tok)
            take_next = True
        elif tok.startswith("--rabbitmq-url="):
            _, _, val = tok.partition("=")
            out.append(f"--rabbitmq-url={_redact_url(val)}")
        else:
            out.append(tok)
    return out


def _setup_logging(namespace: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Configure the triage log."""
    raw = config.get("logging")
    raw = raw if isinstance(raw, dict) else {}

    level_name = str(namespace.log_level or raw.get("level") or DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.WARNING)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    log_file = Path(namespace.log_file or raw.get("file") or DEFAULT_LOG_FILE).expanduser()
    file_handler = next(
        (h for h in logger.handlers if getattr(h, "_libby_cli_handler", False)), None
    )
    if file_handler is None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_file), maxBytes=1_000_000, backupCount=3,
            )
        except OSError:
            file_handler = logging.NullHandler()
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        setattr(file_handler, "_libby_cli_handler", True)
        logger.addHandler(file_handler)
    file_handler.setLevel(level)

    if namespace.verbose and not any(
        getattr(h, "_libby_cli_console", False) for h in logger.handlers
    ):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        setattr(console, "_libby_cli_console", True)
        logger.addHandler(console)


def _mk_libby(namespace: argparse.Namespace, config: Dict[str, Any]) -> Libby:
    transport = resolve_transport(namespace.transport, config)
    self_id = namespace.self_id or DEFAULT_SELF_ID
    if transport == "rabbitmq":
        url = resolve_rabbitmq_url(namespace.rabbitmq_url, config)
        logger.debug(
            "connect self_id=%s transport=rabbitmq url=%s", self_id, _redact_url(url)
        )
        return Libby.rabbitmq(
            self_id=self_id,
            rabbitmq_url=url,
            keys=[],
        )
    address_book = resolve_address_book(config, namespace.addr)
    bind = namespace.bind or DEFAULT_BIND
    logger.debug(
        "connect self_id=%s transport=zmq bind=%s peers=%d",
        self_id, bind, len(address_book),
    )
    return Libby.zmq(
        self_id=self_id,
        bind=bind,
        address_book=address_book,
        keys=[],
        callback=None,
        discover=True,
        discover_interval_s=2.0,
        hello_on_start=True,
    )


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
    logger.warning("error qualified=%s message=%s", qualified, message)
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
        ("timeout_s",   resp.get("timeout_s")),
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


def _modify_timeout(lib: Libby, peer: str, name: str, user_timeout: Optional[float]) -> float:
    """Resolve the timeout for a modify call.

    Precedence: ``--timeout`` flag → ``timeout_s`` from the keyword's
    describe metadata → ``DEFAULT_TIMEOUT_S``.
    """
    if user_timeout is not None:
        return user_timeout
    try:
        resp = _rpc_keys_describe(lib, peer, name, DEFAULT_TIMEOUT_S)
        if resp.get("ok"):
            t = resp.get("timeout_s")
            if t is not None:
                return float(t)
    except Exception:
        pass
    return DEFAULT_TIMEOUT_S


def _peel(envelope: Any) -> Dict[str, Any]:
    """Non-raising unwrap for the renderer: failures become an
    ``{"ok": False, "error": ...}`` dict so a table can show them inline."""
    try:
        return unwrap("", envelope)
    except KeywordError as ex:
        return {"ok": False, "error": ex.error}
    except LibbyError as ex:
        return {"ok": False, "error": str(ex)}


def _rpc_show_one(lib: Libby, peer: str, name: str, timeout: float) -> Dict[str, Any]:
    return _peel(lib.rpc(peer, name, {}, ttl_ms=int(timeout * 1000)))


def _rpc_keys_list(lib: Libby, peer: str, pattern: str, timeout: float) -> Dict[str, Any]:
    return _peel(lib.rpc(peer, "keys.list", {"pattern": pattern}, ttl_ms=int(timeout * 1000)))


def _rpc_keys_describe(lib: Libby, peer: str, name: str, timeout: float) -> Dict[str, Any]:
    return _peel(lib.rpc(peer, "keys.describe", {"name": name}, ttl_ms=int(timeout * 1000)))


def cmd_show(namespace: argparse.Namespace) -> int:
    config = load_cli_config(namespace.config)
    group, scope, name = parse_keyword(namespace.keyword, allow_pattern=True)
    peer = peer_id(group, scope)
    timeout = namespace.timeout if namespace.timeout is not None else DEFAULT_TIMEOUT_S
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
        logger.exception("show %s raised", qualified_arg)
        return _emit_error(qualified_arg, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_list(namespace: argparse.Namespace) -> int:
    config = load_cli_config(namespace.config)
    group, scope, pattern = parse_keyword(namespace.pattern, allow_pattern=True)
    peer = peer_id(group, scope)
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
        logger.exception("list %s raised", namespace.pattern)
        return _emit_error(namespace.pattern, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_describe(namespace: argparse.Namespace) -> int:
    config = load_cli_config(namespace.config)
    group, scope, name = parse_keyword(namespace.keyword, allow_pattern=False)
    peer = peer_id(group, scope)
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
        logger.exception("describe %s raised", qualified)
        return _emit_error(qualified, str(ex), as_json=namespace.json)
    finally:
        if lib is not None:
            try:
                lib.stop()
            except Exception:
                pass


def cmd_modify(namespace: argparse.Namespace) -> int:
    config = load_cli_config(namespace.config)

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

    group, scope, name = parse_keyword(keyword_str)
    peer = peer_id(group, scope)
    qualified = f"{group}.{scope}.{name}"
    value = coerce_value(value_str)

    lib: Optional[Libby] = None
    try:
        lib = _mk_libby(namespace, config)
        timeout = _modify_timeout(lib, peer, name, namespace.timeout)
        resp = _peel(lib.rpc(peer, name, {"value": value}, ttl_ms=int(timeout * 1000)))
        return _emit_one(qualified, resp, as_json=namespace.json)
    except Exception as ex:
        logger.exception("modify %s raised", qualified)
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
    config = load_cli_config(namespace.config)
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
        logger.exception("req peer=%s key=%s raised", namespace.peer, namespace.key)
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
    config = load_cli_config(namespace.config)
    if resolve_transport(namespace.transport, config) != "zmq":
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
        logger.exception("sub topics=%s raised", namespace.topics)
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
                       help=f"Timeout seconds (default: {DEFAULT_TIMEOUT_S}, "
                            f"or the keyword's timeout_s metadata for modify)")
        p.add_argument("--json", action="store_true",
                       help="Emit JSON to stdout instead of the pretty text format")
        p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                       help=f"Triage log level (default: {DEFAULT_LOG_LEVEL}, "
                            "or the config's logging.level)")
        p.add_argument("--log-file",
                       help=f"Triage log file (default: {DEFAULT_LOG_FILE}, "
                            "or the config's logging.file)")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="Also echo the triage log to stderr")

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

    try:
        cli_config = load_cli_config(namespace.config)
    except LibbyError as ex:
        # Core raises LibbyError; the CLI is the boundary that exits
        print(f"libby: {ex}", file=sys.stderr)
        return 2

    _setup_logging(namespace, cli_config)

    argv_display = shlex.join(_redact_argv(argv if argv is not None else sys.argv[1:]))
    logger.info("invoke cmd=%s argv=%s", namespace.cmd, argv_display)

    start = time.monotonic()
    try:
        rc = namespace.func(namespace)
    except LibbyError as ex:
        # Core raises LibbyError; the CLI is the boundary that exits
        logger.warning("cmd=%s failed: %s", namespace.cmd, ex)
        print(f"libby: {ex}", file=sys.stderr)
        rc = 2
    except SystemExit as ex:
        # Hard validation failures (bad keyword syntax, unreadable config)
        logger.warning("cmd=%s aborted: %s", namespace.cmd, ex.code)
        raise
    except Exception:
        logger.exception("cmd=%s crashed", namespace.cmd)
        raise
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("cmd=%s exit=%s elapsed_ms=%.1f", namespace.cmd, rc, elapsed_ms)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
