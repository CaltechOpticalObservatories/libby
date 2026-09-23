"""Keyword-name parsing, value coercion, and peer/group naming (transport-agnostic)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .errors import KeywordNameError


def parse_keyword(arg: str, *, allow_pattern: bool = False) -> Tuple[str, str, str]:
    """Parse '<group>.<daemon>.<keyword>' into (group, daemon, keyword).

    With ``allow_pattern=True``, ``%`` is allowed in the keyword segment.
    Group and daemon must always be explicit.
    """
    parts = arg.split(".", 2)
    if len(parts) < 3 or not all(parts):
        raise KeywordNameError(
            f"address must be <group>.<daemon>.<keyword>, got: {arg}"
        )
    group, daemon, keyword = parts
    if "%" in group or "%" in daemon:
        raise KeywordNameError(
            f"wildcards (%) are not allowed in <group> or <daemon>: {arg}"
        )
    if "%" in keyword and not allow_pattern:
        raise KeywordNameError(
            f"this verb requires an exact keyword (no %): {arg}"
        )
    return group, daemon, keyword


@dataclass(frozen=True)
class AddressPattern:
    """A ``<group>.<daemon>[.<keyword>]`` pattern with ``%`` allowed in every segment."""

    group: str
    daemon: str
    keyword: Optional[str]

    @property
    def spans_peers(self) -> bool:
        """Return True when the group or daemon segment holds a wildcard."""
        return "%" in self.group or "%" in self.daemon

    @property
    def peer_pattern(self) -> str:
        """Return the pattern to match wire ids against, lowercased like the ids."""
        return f"{self.group}.{self.daemon}".lower()


def parse_address_pattern(arg: str) -> AddressPattern:
    """Parse ``<group>.<daemon>[.<keyword>]`` with ``%`` allowed anywhere.

    Unlike :func:`parse_keyword` this admits patterns that span daemons, so it
    is only for callers that resolve them by broadcast (``list``, ``peers``).
    """
    parts = arg.split(".", 2)
    if len(parts) < 2 or not all(parts):
        raise KeywordNameError(
            f"pattern must be <group>.<daemon>[.<keyword>], got: {arg}"
        )
    keyword = parts[2] if len(parts) == 3 else None
    return AddressPattern(parts[0], parts[1], keyword)


def qualified_peer_id(peer_id: str, group_id: Optional[str] = None) -> str:
    """Return the lowercased wire identity "<group_id>.<peer_id>", or bare peer_id.

    The single choke point both daemon and client build their identity from,
    so a short peer_id cannot collide across groups and case never matters.
    """
    peer_id = peer_id.lower()
    if group_id:
        return f"{group_id.lower()}.{peer_id}"
    return peer_id


def peer_id(group: str, daemon: str) -> str:
    """Map an address's group/daemon segments to the daemon peer id.

    `daemon` is the peer_id, `group` is its group_id: the same pair
    `qualified_peer_id` joins on the daemon side, so client and daemon
    always agree on the wire identity by construction. See
    plans/peer_group_naming_design.md.
    """
    return qualified_peer_id(daemon, group)


_LITERALS = {"null": None, "true": True, "false": False}


def coerce_value(value: str) -> Any:
    """Coerce a modify value string.

    Empty / 'null' → None; 'true'/'false' → bool; parseable as int → int;
    parseable as float → float; otherwise the original string.
    """
    if value == "":
        return None
    low = value.strip().lower()
    if low in _LITERALS:
        return _LITERALS[low]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
