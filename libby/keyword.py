"""Keyword classes for Libby."""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, List, Optional

Getter = Callable[[], Any]
Setter = Callable[[Any], None]
Validator = Callable[[Any], Optional[str]]
Action = Callable[[], None]


def match_pattern(pattern: str, names: Iterable[str]) -> List[str]:
    """Return names matching ``pattern``, sorted. ``%`` is a wildcard;
    it matches any run of characters excluding ``.``."""
    parts = pattern.split("%")
    rx = re.compile("^" + "[^.]*".join(re.escape(p) for p in parts) + "$")
    return sorted(n for n in names if rx.match(n))


class Keyword:
    """Named value exposed as a libby service."""

    type_name: str = "any"

    def __init__(
        self,
        name: str,
        *,
        getter: Optional[Getter] = None,
        setter: Optional[Setter] = None,
        units: Optional[str] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Validator] = None,
    ) -> None:
        if getter is None and setter is None:
            raise ValueError(
                f"keyword '{name}' must supply at least one of getter/setter"
            )
        self.name = name
        self._getter = getter
        self._setter = setter
        self.units = units
        self.description = description
        self.nullable = nullable
        self._validator = validator

    @property
    def readonly(self) -> bool:
        return self._setter is None

    @property
    def writeonly(self) -> bool:
        return self._getter is None

    def _type_check(self, v: Any) -> Any:
        """Type-check or cast ``v``. Override per subclass."""
        return v

    def _response(self, value: Any) -> dict:
        out: dict[str, Any] = {"ok": True, "value": value}
        if self.units is not None:
            out["units"] = self.units
        return out

    def describe(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type_name,
            "readonly": self.readonly,
            "writeonly": self.writeonly,
            "nullable": self.nullable,
        }
        if self.units is not None:
            out["units"] = self.units
        if self.description:
            out["description"] = self.description
        return out

    def handle(self, payload: dict) -> dict:
        if "value" in payload:
            return self._modify(payload["value"])
        return self._show()

    def _show(self) -> dict:
        if self.writeonly:
            return {"ok": False, "error": "keyword is write-only"}
        try:
            value = self._getter()  # type: ignore[misc]
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self._response(value)

    def _modify(self, raw: Any) -> dict:
        if self.readonly:
            return {"ok": False, "error": "keyword is read-only"}
        if raw is None:
            if not self.nullable:
                return {"ok": False, "error": f"value must be {self.type_name}"}
            value: Any = None
        else:
            try:
                value = self._type_check(raw)
            except (TypeError, ValueError) as e:
                return {"ok": False, "error": str(e)}
            if self._validator is not None:
                verr = self._validator(value)
                if verr:
                    return {"ok": False, "error": verr}
        try:
            self._setter(value)  # type: ignore[misc]
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self._response(value)


class BoolKeyword(Keyword):
    """Keyword whose value is a Python ``bool``."""

    type_name = "bool"

    def _type_check(self, v: Any) -> bool:
        if not isinstance(v, bool):
            raise TypeError("value must be a bool")
        return v


class IntKeyword(Keyword):
    """Keyword whose value is a Python ``int``; ``bool`` is rejected."""

    type_name = "int"

    def _type_check(self, v: Any) -> int:
        # bool is a subclass of int in Python; reject explicitly
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError("value must be an int")
        return v


class FloatKeyword(Keyword):
    """Keyword whose value is a Python ``float``; accepts ``int``, rejects ``bool``."""

    type_name = "float"

    def _type_check(self, v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError("value must be a number")
        return float(v)


class StringKeyword(Keyword):
    """Keyword whose value is a Python ``str``."""

    type_name = "string"

    def _type_check(self, v: Any) -> str:
        if not isinstance(v, str):
            raise TypeError("value must be a string")
        return v


class TriggerKeyword(Keyword):
    """Write-only action keyword. Any modify fires ``action``; show returns ``false``."""

    type_name = "trigger"

    def __init__(
        self,
        name: str,
        *,
        action: Action,
        description: str = "",
    ) -> None:
        self.name = name
        self._action = action
        self._getter = None
        self._setter = None
        self.units = None
        self.description = description
        self.nullable = False
        self._validator = None

    @property
    def readonly(self) -> bool:
        return False

    @property
    def writeonly(self) -> bool:
        return True

    def handle(self, payload: dict) -> dict:
        if "value" not in payload:
            return {"ok": True, "value": False}
        try:
            self._action()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "value": True}


__all__ = [
    "Keyword",
    "BoolKeyword",
    "IntKeyword",
    "FloatKeyword",
    "StringKeyword",
    "TriggerKeyword",
    "match_pattern",
]
