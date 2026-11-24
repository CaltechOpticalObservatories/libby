"""Keyword classes for Libby."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union

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


FIELD_TYPES = ("bool", "int", "float", "string", "any")
"""The type names a :class:`Field` accepts."""


@dataclass(frozen=True)
class Field:
    """One field of a :class:`DictKeyword` schema.

    ``type`` is one of :data:`FIELD_TYPES`; ``"any"`` accepts any JSON value
    unchecked. A field that is not ``required`` may be left out, and is then
    absent from the dict the setter receives rather than filled with ``None``.
    """

    type: str
    units: Optional[str] = None
    description: str = ""
    required: bool = True
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(
                f"unknown field type {self.type!r}; "
                f"expected one of: {', '.join(FIELD_TYPES)}"
            )

    def check(self, v: Any) -> Any:
        """Type-check or cast one field value; raise TypeError if it does not fit.

        Mirrors the scalar keyword types' ``_type_check``, messages included.
        """
        if v is None:
            if self.nullable:
                return None
            raise TypeError("value must not be null")
        if self.type == "bool":
            if not isinstance(v, bool):
                raise TypeError("value must be a bool")
        elif self.type == "int":
            # bool is a subclass of int in Python; reject explicitly
            if isinstance(v, bool) or not isinstance(v, int):
                raise TypeError("value must be an int")
        elif self.type == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise TypeError("value must be a number")
            return float(v)
        elif self.type == "string":
            if not isinstance(v, str):
                raise TypeError("value must be a string")
        return v

    def describe(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "type": self.type,
            "required": self.required,
            "nullable": self.nullable,
        }
        if self.units is not None:
            out["units"] = self.units
        if self.description:
            out["description"] = self.description
        return out


FieldSpec = Union[str, Field]


def _quoted(names: Iterable[str]) -> str:
    return ", ".join(f"'{n}'" for n in names)


def _normalize_fields(keyword: str, fields: Mapping[str, FieldSpec]) -> Dict[str, Field]:
    """Turn a ``fields`` argument into name → Field, failing at construction."""
    out: Dict[str, Field] = {}
    for name, spec in fields.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"keyword '{keyword}': field names must be non-empty strings")
        if isinstance(spec, str):
            try:
                spec = Field(spec)
            except ValueError as e:
                raise ValueError(f"keyword '{keyword}': field '{name}': {e}") from e
        elif not isinstance(spec, Field):
            raise TypeError(
                f"keyword '{keyword}': field '{name}' must be a type name or a Field"
            )
        out[name] = spec
    return out


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
        timeout_s: Optional[float] = None,
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
        self.timeout_s = timeout_s

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
        if self.timeout_s is not None:
            out["timeout_s"] = self.timeout_s
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


class DictKeyword(Keyword):
    """Keyword whose value is a JSON object: a ``dict`` with ``str`` keys.

    Without ``fields`` any such dict is accepted. With ``fields``, a mapping of
    field name to a type name (see :data:`FIELD_TYPES`) or a :class:`Field`,
    a modify must name only known fields, include every required one, and give
    each a value of its type; ``float`` fields cast ints as
    :class:`FloatKeyword` does. ``validator`` runs after these checks, for
    rules spanning fields. The setter receives a new dict, never the caller's
    payload object. ``describe()`` advertises the schema under ``fields``.
    """

    type_name = "dict"

    def __init__(
        self,
        name: str,
        *,
        getter: Optional[Getter] = None,
        setter: Optional[Setter] = None,
        fields: Optional[Mapping[str, FieldSpec]] = None,
        units: Optional[str] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Validator] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        super().__init__(
            name,
            getter=getter, setter=setter, units=units, description=description,
            nullable=nullable, validator=validator, timeout_s=timeout_s,
        )
        self.fields: Optional[Dict[str, Field]] = (
            None if fields is None else _normalize_fields(name, fields)
        )

    def _type_check(self, v: Any) -> Dict[str, Any]:
        if not isinstance(v, dict):
            raise TypeError("value must be a dict")
        if not all(isinstance(k, str) for k in v):
            raise TypeError("dict keys must be strings")
        if self.fields is None:
            return dict(v)

        unknown = [k for k in v if k not in self.fields]
        if unknown:
            noun = "field" if len(unknown) == 1 else "fields"
            raise ValueError(
                f"unknown {noun} {_quoted(unknown)}; expected {_quoted(self.fields)}"
            )
        missing = [k for k, f in self.fields.items() if f.required and k not in v]
        if missing:
            noun = "field" if len(missing) == 1 else "fields"
            raise ValueError(f"missing required {noun} {_quoted(missing)}")

        out: Dict[str, Any] = {}
        for key, raw in v.items():
            try:
                out[key] = self.fields[key].check(raw)
            except (TypeError, ValueError) as e:
                raise TypeError(f"field '{key}': {e}") from e
        return out

    def describe(self) -> dict:
        out = super().describe()
        if self.fields is not None:
            out["fields"] = {n: f.describe() for n, f in self.fields.items()}
        return out


class TriggerKeyword(Keyword):
    """Write-only action keyword. Any modify fires ``action``; show returns ``false``."""

    type_name = "trigger"

    def __init__(
        self,
        name: str,
        *,
        action: Action,
        description: str = "",
        timeout_s: Optional[float] = None,
    ) -> None:
        self.name = name
        self._action = action
        self._getter = None
        self._setter = None
        self.units = None
        self.description = description
        self.nullable = False
        self._validator = None
        self.timeout_s = timeout_s

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
    "DictKeyword",
    "Field",
    "FieldSpec",
    "FIELD_TYPES",
    "TriggerKeyword",
    "match_pattern",
]
