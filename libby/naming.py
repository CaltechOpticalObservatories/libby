"""Keyword-name parsing and value coercion shared by the libby CLI and lib."""
from __future__ import annotations

from typing import Any, Tuple

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


def peer_id(group: str, scope: str) -> str:
    """Map a keyword's group/scope to the daemon peer id."""
    return f"{group}.{scope}"


def coerce_value(value: str) -> Any:
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
