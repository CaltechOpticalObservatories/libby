"""Shell completion for libby addresses, fed by one cached broadcast ``keys.list``.

The listing comes from the same broadcast ``libby list`` uses, not from
bamboo's hello/discovery, which never reports who is alive.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from libby.keyword import match_pattern

DEFAULT_CACHE_PATH = Path.home() / ".libby" / "completion_cache.json"
CACHE_TTL_S = 10.0
COMPLETE_TIMEOUT_S = 0.5

Listings = Dict[str, List[str]]
ListingsSource = Callable[[float], Listings]


@dataclass(frozen=True)
class CompletionCache:
    """Last peer listing on disk, so a burst of TABs costs one broadcast."""

    path: Path = DEFAULT_CACHE_PATH
    ttl_s: float = CACHE_TTL_S

    def load(self, connection: str) -> Optional[Listings]:
        """Return the cached listings for ``connection`` while still fresh."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("connection") != connection:
            return None
        if time.time() - float(raw.get("ts", 0.0)) > self.ttl_s:
            return None
        listings = raw.get("listings")
        return listings if isinstance(listings, dict) else None

    def store(self, connection: str, listings: Listings) -> None:
        """Write the listings for ``connection``; an unwritable cache just means no cache."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"connection": connection, "ts": time.time(), "listings": listings}),
                encoding="utf-8",
            )
        except OSError:
            pass


def cached_listings(connection: str, fetch: ListingsSource, cache: CompletionCache) -> Listings:
    """Return the listings from the cache, or fetch, store and return fresh ones."""
    listings = cache.load(connection)
    if listings is None:
        listings = fetch(COMPLETE_TIMEOUT_S)
        cache.store(connection, listings)
    return listings


def peer_candidates(prefix: str, listings: Listings) -> List[str]:
    """Complete a partial ``<group>.<daemon>``."""
    return sorted(peer for peer in listings if peer.startswith(prefix.lower()))


def address_candidates(prefix: str, listings: Listings) -> List[str]:
    """Complete a partial ``<group>.<daemon>.<keyword>``.

    Until the daemon segment is complete the candidates end in ``.``, so the
    shell stops there and the next TAB moves on to the keywords.
    """
    if prefix.count(".") < 2:
        peers = peer_candidates(prefix, listings)
        # The shell appends a space to a lone candidate unless it ends in
        # =/:, so an unambiguous daemon expands to its keywords right away
        if len(peers) != 1:
            return [f"{peer}." for peer in peers]
        prefix = f"{peers[0]}."
    peer, _, keyword_prefix = prefix.rpartition(".")
    peer = peer.lower()
    return sorted(
        f"{peer}.{name}"
        for name in listings.get(peer, [])
        if name.startswith(keyword_prefix)
    )


def _peer_lowered(prefix: str) -> str:
    """Lowercase the group and daemon segments, which are case-insensitive."""
    group, dot, rest = prefix.partition(".")
    daemon, dot2, keyword = rest.partition(".")
    return f"{group.lower()}{dot}{daemon.lower()}{dot2}{keyword}"


def list_candidates(prefix: str, listings: Listings) -> List[str]:
    """Complete a ``libby list`` pattern.

    Unlike an address, the daemon segment may stand alone and any segment may
    be ``%``, so each level offers its wildcard next to the concrete names.
    """
    prefix = _peer_lowered(prefix)
    if prefix.count(".") < 2:
        peers = peer_candidates(prefix, listings)
        groups = sorted({peer.split(".", 1)[0] for peer in peers})
        candidates = ["%.%", *(f"{group}.%" for group in groups), *peers]
    else:
        peer_pattern, _, _ = prefix.rpartition(".")
        candidates = [f"{peer_pattern}.%"] + [
            f"{peer_pattern}.{name}"
            for peer in match_pattern(peer_pattern, listings)
            for name in listings[peer]
        ]
    return sorted({c for c in candidates if c.startswith(prefix)})
