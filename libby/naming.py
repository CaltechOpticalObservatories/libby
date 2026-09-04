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
    """Map a keyword's group/scope to the daemon peer id.

    Underscore, not dot: every deployed daemon config sets peer_id this way
    (e.g. hsfei_pickoff), and it's what the CLI/lib addressing scheme was
    designed against (see plans/libby_lib_design.md).
    """
    return f"{group}_{scope}"


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
