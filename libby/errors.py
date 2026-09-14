"""Exceptions raised by libby core and lib code.

Core and library code raises these errors. Entry points (the
CLI or a daemon) catch them and decide whether to exit the process or bubble up.
"""
from __future__ import annotations


class LibbyError(Exception):
    """Base class for all libby errors."""


class ConfigError(LibbyError):
    """A config file or connection setting is invalid."""


class KeywordNameError(LibbyError):
    """A qualified keyword name is malformed."""


class ExpressionError(LibbyError):
    """A ``wait_for`` expression is malformed, or cannot be evaluated."""


class LibbyTimeout(LibbyError):
    """An RPC request was not delivered, or got no response, within its TTL."""


class KeywordError(LibbyError):
    """A daemon rejected a keyword get/set (responded with ``ok=False``)."""

    def __init__(self, name: str, error: str) -> None:
        self.name = name
        self.error = error
        super().__init__(f"{name}: {error}")
