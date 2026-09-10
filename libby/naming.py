"""Keyword-name parsing, value coercion, and peer/group naming (transport-agnostic)."""
from __future__ import annotations

from typing import Any, Optional, Tuple

from .errors import KeywordNameError


def parse_keyword(arg: str, *, allow_pattern: bool = False) -> Tuple[str, str, str]:
    """Parse '<group>.<scope>.<name>' into (group, scope, name).

    With ``allow_pattern=True``, ``%`` is allowed in the name segment.
    Group and scope must always be explicit.
    """
    parts = arg.split(".", 2)
    if len(parts) < 3 or not all(parts):
        raise KeywordNameError(f"keyword must be <group>.<scope>.<name>, got: {arg}")
    group, scope, name = parts
    if "%" in group or "%" in scope:
        raise KeywordNameError(
            f"wildcards (%) are not allowed in <group> or <scope>: {arg}"
        )
    if "%" in name and not allow_pattern:
        raise KeywordNameError(
            f"this verb requires an exact keyword name (no %): {arg}"
        )
    return group, scope, name


def qualified_peer_id(peer_id: str, group_id: Optional[str] = None) -> str:
    """Return the wire identity for `peer_id`, namespaced by `group_id`.

    Joins as "<group_id>.<peer_id>" when group_id is set, else returns
    peer_id unchanged. Computed once here and handed to a transport as an
    already-unique opaque string, rather than teaching each transport
    implementation to be group-aware on its own: a short, human peer_id like
    "adc" is only safe from cross-group collisions once whatever's actually
    used on the wire (a routing key, a DEALER identity, or any future
    transport's own addressing) includes the group. See
    plans/peer_group_naming_design.md.

    Lowercases both inputs, so addressing is case-insensitive without either
    side (daemon or client) needing its own normalization: both go through
    this one function to build the identity they route on.
    """
    peer_id = peer_id.lower()
    if group_id:
        return f"{group_id.lower()}.{peer_id}"
    return peer_id


def peer_id(group: str, scope: str) -> str:
    """Map a keyword's group/scope to the daemon peer id.

    `scope` is the peer_id, `group` is its group_id: the same pair
    `qualified_peer_id` joins on the daemon side, so client and daemon
    always agree on the wire identity by construction. See
    plans/peer_group_naming_design.md.
    """
    return qualified_peer_id(scope, group)


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
