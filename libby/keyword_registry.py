"""Per-Libby buffer for building Keyword instances with a typed-method API."""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional

from .keyword import (
    BoolKeyword,
    FloatKeyword,
    IntKeyword,
    Keyword,
    StringKeyword,
    TriggerKeyword,
)


class KeywordRegistry:
    """Collects Keyword instances built via typed methods.

    Build keywords with ``bool`` / ``int`` / ``float`` / ``string`` /
    ``trigger``; each call appends to an internal buffer. Call
    ``drain()`` to retrieve and clear the buffer; pass the result to
    ``Libby.register_keywords``.
    """

    def __init__(self) -> None:
        self._keywords_to_register: List[Keyword] = []

    def _add(self, keyword: Keyword) -> Keyword:
        self._keywords_to_register.append(keyword)
        return keyword

    def bool(
        self,
        name: str,
        *,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Callable[[Any], Optional[str]]] = None,
        timeout_s: Optional[float] = None,
    ) -> BoolKeyword:
        return self._add(BoolKeyword(
            name,
            getter=getter, setter=setter,
            description=description, nullable=nullable, validator=validator,
            timeout_s=timeout_s,
        ))

    def int(
        self,
        name: str,
        *,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        units: Optional[str] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Callable[[Any], Optional[str]]] = None,
        timeout_s: Optional[float] = None,
    ) -> IntKeyword:
        return self._add(IntKeyword(
            name,
            getter=getter, setter=setter, units=units,
            description=description, nullable=nullable, validator=validator,
            timeout_s=timeout_s,
        ))

    def float(
        self,
        name: str,
        *,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        units: Optional[str] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Callable[[Any], Optional[str]]] = None,
        timeout_s: Optional[float] = None,
    ) -> FloatKeyword:
        return self._add(FloatKeyword(
            name,
            getter=getter, setter=setter, units=units,
            description=description, nullable=nullable, validator=validator,
            timeout_s=timeout_s,
        ))

    def string(
        self,
        name: str,
        *,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        description: str = "",
        nullable: bool = False,
        validator: Optional[Callable[[Any], Optional[str]]] = None,
        timeout_s: Optional[float] = None,
    ) -> StringKeyword:
        return self._add(StringKeyword(
            name,
            getter=getter, setter=setter,
            description=description, nullable=nullable, validator=validator,
            timeout_s=timeout_s,
        ))

    def trigger(
        self,
        name: str,
        *,
        action: Callable[[], None],
        description: str = "",
        timeout_s: Optional[float] = None,
    ) -> TriggerKeyword:
        return self._add(TriggerKeyword(
            name, action=action, description=description, timeout_s=timeout_s,
        ))

    def add(self, keyword: Keyword) -> Keyword:
        """Buffer a Keyword built directly (e.g. via a stage helper)."""
        return self._add(keyword)

    def add_all(self, keywords: Iterable[Keyword]) -> None:
        """Buffer many keywords at once."""
        for keyword in keywords:
            self._add(keyword)

    def drain(self) -> List[Keyword]:
        """Return the buffered keywords and clear the buffer."""
        out, self._keywords_to_register = self._keywords_to_register, []
        return out


__all__ = ["KeywordRegistry"]
