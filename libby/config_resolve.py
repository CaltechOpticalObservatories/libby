"""Resolve libby connection settings from cli_config.yaml plus overrides.

Shared by the libby CLI and the lib's ``Client.from_config`` so the precedence
rules (override → config file → built-in default) live in one place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .errors import ConfigError

DEFAULT_BIND = "tcp://127.0.0.1:56001"
DEFAULT_RABBITMQ_URL = "amqp://localhost"
DEFAULT_TRANSPORT = "rabbitmq"
DEFAULT_CONFIG_PATH = Path.home() / ".libby" / "cli_config.yaml"


def load_cli_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the client's cli_config.yaml. Missing file → empty dict.

    The client config is optional, so it wraps ``libby.config.load_config`` with
    ``optional=True`` and re-raises parse/shape failures as ``ConfigError``.
    """
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        return config.load_config(target, optional=True)
    except Exception as ex:
        raise ConfigError(f"failed to load {target}: {ex}") from ex


def resolve_transport(override: Optional[str], config: Dict[str, Any]) -> str:
    """Pick the transport: override → config → default; validated."""
    transport = override or config.get("transport") or DEFAULT_TRANSPORT
    if transport not in ("zmq", "rabbitmq"):
        raise ConfigError(f"invalid transport: {transport}")
    return transport


def resolve_rabbitmq_url(override: Optional[str], config: Dict[str, Any]) -> str:
    """Pick the RabbitMQ URL: override → config → default."""
    return override or config.get("rabbitmq_url") or DEFAULT_RABBITMQ_URL


def parse_addr_kv(entry: str) -> Tuple[str, str]:
    """Parse a 'peer_id=tcp://host:port' address-book entry."""
    if "=" not in entry:
        raise ConfigError("Expected 'peer_id=tcp://host:port'")
    peer, address = entry.split("=", 1)
    peer, address = peer.strip(), address.strip()
    if not peer or not address:
        raise ConfigError("Expected 'peer_id=tcp://host:port'")
    return peer, address


def resolve_address_book(
    config: Dict[str, Any],
    extra_addrs: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Merge the config 'peers' map with extra 'peer=addr' override strings."""
    book: Dict[str, str] = dict(config.get("peers") or {})
    for entry in (extra_addrs or []):
        peer, address = parse_addr_kv(entry)
        book[peer] = address
    return book
