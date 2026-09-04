"""Shared peer/group naming, transport-agnostic."""
from __future__ import annotations

from typing import Optional


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
    """
    if group_id:
        return f"{group_id}.{peer_id}"
    return peer_id
