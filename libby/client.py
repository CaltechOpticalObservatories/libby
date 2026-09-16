"""Programmatic client for getting and setting keywords on libby daemons.

``Client`` CLI spins up a connection and holds it for its lifetime, so a script can touch many
keywords cheaply. It resolves a qualified ``<group>.<daemon>.<keyword>`` to a peer
and key, calls ``Libby.rpc``, and turns the reply into a value or a
``LibbyError`` via :func:`libby.response.unwrap`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .config_resolve import (
    DEFAULT_BIND,
    load_cli_config,
    resolve_address_book,
    resolve_rabbitmq_url,
    resolve_transport,
)
from .errors import LibbyError, LibbyTimeout
from .expression import parse_comparison
from .libby import Libby
from .naming import parse_keyword, peer_id
from .response import unwrap

DEFAULT_SELF_ID = "libby-client"
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_POLL_S = 0.1


@dataclass(frozen=True)
class WaitResult:
    """What a :meth:`Client.wait_for_result` wait observed before it stopped."""

    satisfied: bool
    """True if the expression became true; False if the timeout expired."""

    address: str
    """``<group>.<daemon>.<keyword>`` address the expression polled."""

    value: Any
    """Last value read. ``None`` if no read ever succeeded."""

    elapsed_s: float
    """Wall-clock seconds spent waiting."""

    polls: int
    """Number of reads attempted."""

# Keeps one ``keys.read`` response well inside the transports' 512 KB MTU,
# however many keywords a caller asks for at once.
DEFAULT_READ_CHUNK = 100


def _chunked(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass(frozen=True)
class KeyListing:
    """One peer's answer to a ``keys.list`` request.

    ``services`` lists the non-keyword keys the peer answers, such as
    ``keys.read``. It is empty for a peer on a libby old enough not to report
    the field, which is how a caller decides whether bulk reads are available:
    an unknown key is dropped without an ACK, so probing for one looks exactly
    like a dead peer.
    """

    names: Tuple[str, ...]
    services: Tuple[str, ...]


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
        group, daemon, keyword = parse_keyword(name)
        envelope = self._libby.rpc(peer_id(group, daemon), keyword, {},
                                   ttl_ms=int(timeout_s * 1000))
        return unwrap(name, envelope)

    def get(self, name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Any:
        """Read a keyword's value."""
        return self.show(name, timeout_s=timeout_s).get("value")

    def set(self, name: str, value: Any, *, timeout_s: Optional[float] = None) -> Any:
        """Write a keyword and return the value the daemon applied."""
        group, daemon, keyword = parse_keyword(name)
        ttl_s = self._set_timeout(name, timeout_s)
        envelope = self._libby.rpc(peer_id(group, daemon), keyword, {"value": value},
                                   ttl_ms=int(ttl_s * 1000))
        return unwrap(name, envelope).get("value")

    def listing(self, pattern: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> KeyListing:
        """List a peer's matching keywords and the services it serves, in one call.

        Use this over :meth:`list` when the caller also needs to know whether
        the peer supports bulk reads; both come from the same ``keys.list``
        response, so asking costs no extra round trip.
        """
        group, daemon, keyword_pattern = parse_keyword(pattern, allow_pattern=True)
        envelope = self._libby.rpc(peer_id(group, daemon), "keys.list",
                                   {"pattern": keyword_pattern},
                                   ttl_ms=int(timeout_s * 1000))
        response = unwrap(pattern, envelope)
        return KeyListing(
            names=tuple(f"{group}.{daemon}.{match}"
                        for match in response.get("matches", [])),
            services=tuple(response.get("services", [])),
        )

    def list(self, pattern: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> List[str]:
        """List qualified keyword names matching ``<group>.<daemon>.<pattern>``.

        Returns fully qualified names, so the result feeds straight back into
        :meth:`get`, :meth:`show` or :meth:`read`.
        """
        return list(self.listing(pattern, timeout_s=timeout_s).names)

    def describe(self, name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
        """Read one keyword's metadata (type, access, units, timeout_s)."""
        group, daemon, keyword = parse_keyword(name)
        envelope = self._libby.rpc(peer_id(group, daemon), "keys.describe",
                                   {"name": keyword}, ttl_ms=int(timeout_s * 1000))
        return unwrap(name, envelope)

    def read(
        self,
        names: Sequence[str],
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        chunk_size: int = DEFAULT_READ_CHUNK,
    ) -> Dict[str, Dict[str, Any]]:
        """Read many keywords using one request per peer, per chunk.

        Unlike :meth:`get` and :meth:`set`, this never raises for a failed
        read: every requested name maps to its own response dict, so one dead
        peer or one broken getter costs only its own entries. Names may span
        peers; each peer is requested separately, in the order given.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")

        by_peer: Dict[Tuple[str, str], List[str]] = {}
        for name in names:
            group, daemon, keyword = parse_keyword(name)
            by_peer.setdefault((group, daemon), []).append(keyword)

        results: Dict[str, Dict[str, Any]] = {}
        for (group, daemon), keywords in by_peer.items():
            for chunk in _chunked(keywords, chunk_size):
                results.update(self._read_chunk(group, daemon, chunk, timeout_s))
        return results

    def _read_chunk(
        self,
        group: str,
        daemon: str,
        keywords: Sequence[str],
        timeout_s: float,
    ) -> Dict[str, Dict[str, Any]]:
        """Read one peer's keywords, reporting a peer-level failure per name."""
        prefix = f"{group}.{daemon}"
        try:
            envelope = self._libby.rpc(peer_id(group, daemon), "keys.read",
                                       {"names": list(keywords)},
                                       ttl_ms=int(timeout_s * 1000))
            values = unwrap(prefix, envelope).get("values", {})
        except LibbyError as exc:
            # Spread a peer-level failure across its names so a dead peer
            # cannot hide the peers that did answer
            return {f"{prefix}.{keyword}": {"ok": False, "error": str(exc)}
                    for keyword in keywords}
        return {
            f"{prefix}.{keyword}": values.get(
                keyword, {"ok": False, "error": "missing from keys.read response"})
            for keyword in keywords
        }

    def _set_timeout(self, name: str, override: Optional[float]) -> float:
        """Resolve a write timeout: override → keyword's timeout_s → default."""
        if override is not None:
            return override
        try:
            described = self.describe(name).get("timeout_s")
            if described is not None:
                return float(described)
        except LibbyError:
            # Best-effort: a missing/unreadable describe just falls back
            pass
        return DEFAULT_TIMEOUT_S

    def wait_for(
        self,
        expression: str,
        timeout: Optional[float] = None,
        *,
        daemon: Optional[str] = None,
        case: bool = False,
        poll_s: float = DEFAULT_POLL_S,
        rpc_timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> bool:
        """Block until ``expression`` is true; return whether it became true.

        ``expression`` is one comparison between a ``$``-prefixed keyword and
        a literal — see :mod:`libby.expression` for the accepted syntax::

            client.wait_for('$hsfei.pickoff.positionvalue > 15', timeout=5)
            client.wait_for('$ismoving == false', 30, daemon='hsfei.pickoff')

        Args:
            expression: The condition to wait on.
            timeout: Seconds to wait before giving up. ``None`` waits
                indefinitely; ``0`` evaluates once and returns.
            daemon: Default ``<group>.<daemon>``, so the expression can
                name a keyword bare.
            case: Compare strings case-sensitively.
            poll_s: Seconds between reads.
            rpc_timeout_s: Per-read RPC timeout.

        Returns:
            True if the expression became true, False if the timeout expired
            with it still false.

        Raises:
            ExpressionError: the expression is malformed, or its two sides
                cannot be compared at all.
            KeywordError: the daemon rejected the read (e.g. unknown or
                write-only keyword) — a condition waiting cannot resolve.
        """
        return self.wait_for_result(
            expression,
            timeout,
            daemon=daemon,
            case=case,
            poll_s=poll_s,
            rpc_timeout_s=rpc_timeout_s,
        ).satisfied

    def wait_for_result(
        self,
        expression: str,
        timeout: Optional[float] = None,
        *,
        daemon: Optional[str] = None,
        case: bool = False,
        poll_s: float = DEFAULT_POLL_S,
        rpc_timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> WaitResult:
        """Like :meth:`wait_for`, but report what the wait observed.

        Same arguments and same exceptions; returns a :class:`WaitResult`
        instead of a bool, for callers that want to show the value the
        expression settled on (or timed out against).
        """
        comparison = parse_comparison(expression, daemon=daemon)
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        value: Any = None
        polls = 0

        while True:
            polls += 1
            try:
                value = self.get(comparison.address, timeout_s=rpc_timeout_s)
            except LibbyTimeout:
                # Transient: a restarting daemon shouldn't end a wait early.
                # Keep the last value and retry until the caller's timeout.
                pass
            else:
                if comparison.evaluate(value, case=case):
                    return WaitResult(
                        satisfied=True,
                        address=comparison.address,
                        value=value,
                        elapsed_s=time.monotonic() - start,
                        polls=polls,
                    )

            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return WaitResult(
                    satisfied=False,
                    address=comparison.address,
                    value=value,
                    elapsed_s=now - start,
                    polls=polls,
                )
            nap = poll_s if deadline is None else min(poll_s, deadline - now)
            if nap > 0:
                time.sleep(nap)

    def close(self) -> None:
        """Disconnect the underlying transport."""
        self._libby.stop()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
