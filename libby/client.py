"""Programmatic client for getting and setting keywords on libby daemons.

``Client`` CLI spins up a connection and holds it for its lifetime, so a script can touch many
keywords cheaply. It resolves a qualified ``<group>.<scope>.<name>`` to a peer
and key, calls ``Libby.rpc``, and turns the reply into a value or a
``LibbyError`` via :func:`libby.response.unwrap`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .config_resolve import (
    DEFAULT_BIND,
    load_cli_config,
    resolve_address_book,
    resolve_rabbitmq_url,
    resolve_transport,
)
from .errors import LibbyError
from .libby import Libby
from .naming import parse_keyword, peer_id
from .response import unwrap

DEFAULT_SELF_ID = "libby-client"
DEFAULT_TIMEOUT_S = 3.0


class Client:
    """Long-lived, in-process handle for getting and setting libby keywords."""

    def __init__(self, libby: Libby) -> None:
        self._libby = libby

    @classmethod
    def rabbitmq(
        cls,
        *,
        self_id: str = DEFAULT_SELF_ID,
        rabbitmq_url: str = "amqp://localhost",
    ) -> "Client":
        """Connect over RabbitMQ."""
        return cls(Libby.rabbitmq(self_id=self_id, rabbitmq_url=rabbitmq_url, keys=[]))

    @classmethod
    def zmq(
        cls,
        *,
        self_id: str = DEFAULT_SELF_ID,
        bind: str = DEFAULT_BIND,
        address_book: Optional[Dict[str, str]] = None,
    ) -> "Client":
        """Connect over ZMQ, given an address book of peer endpoints."""
        return cls(Libby.zmq(
            self_id=self_id,
            bind=bind,
            address_book=address_book or {},
            keys=[],
            discover=True,
            discover_interval_s=2.0,
            hello_on_start=True,
        ))

    @classmethod
    def from_config(
        cls,
        path: Optional[str] = None,
        *,
        self_id: str = DEFAULT_SELF_ID,
    ) -> "Client":
        """Build a Client from cli_config.yaml, resolving transport as the CLI does."""
        config = load_cli_config(path)
        if resolve_transport(None, config) == "rabbitmq":
            return cls.rabbitmq(self_id=self_id,
                                rabbitmq_url=resolve_rabbitmq_url(None, config))
        return cls.zmq(self_id=self_id, address_book=resolve_address_book(config))

    def show(self, name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
        """Read a keyword's full response (value, units, flags)."""
        group, scope, keyword = parse_keyword(name)
        envelope = self._libby.rpc(peer_id(group, scope), keyword, {},
                                   ttl_ms=int(timeout_s * 1000))
        return unwrap(name, envelope)

    def get(self, name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Any:
        """Read a keyword's value."""
        return self.show(name, timeout_s=timeout_s).get("value")

    def set(self, name: str, value: Any, *, timeout_s: Optional[float] = None) -> Any:
        """Write a keyword and return the value the daemon applied."""
        group, scope, keyword = parse_keyword(name)
        peer = peer_id(group, scope)
        ttl_s = self._set_timeout(name, peer, keyword, timeout_s)
        envelope = self._libby.rpc(peer, keyword, {"value": value},
                                   ttl_ms=int(ttl_s * 1000))
        return unwrap(name, envelope).get("value")

    def _set_timeout(
        self,
        name: str,
        peer: str,
        keyword: str,
        override: Optional[float],
    ) -> float:
        """Resolve a write timeout: override → keyword's timeout_s → default."""
        if override is not None:
            return override
        try:
            envelope = self._libby.rpc(peer, "keys.describe", {"name": keyword},
                                       ttl_ms=int(DEFAULT_TIMEOUT_S * 1000))
            described = unwrap(name, envelope).get("timeout_s")
            if described is not None:
                return float(described)
        except LibbyError:
            # Best-effort: a missing/unreadable describe just falls back
            pass
        return DEFAULT_TIMEOUT_S

    def close(self) -> None:
        """Disconnect the underlying transport."""
        self._libby.stop()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
