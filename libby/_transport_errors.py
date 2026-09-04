"""Shared helper for building transport setup-error messages."""
from __future__ import annotations


def describe_exception(exc: BaseException) -> str:
    """Return a human-readable message for `exc`.

    Some exceptions (e.g. pika's AMQPConnectionError on a refused connection)
    have an empty str() and bury the real cause in repr()/args instead, which
    turns "transport setup failed" into a message with nothing after the
    colon. Fall back to repr() whenever str() has nothing useful to say.
    """
    text = str(exc)
    return text if text else repr(exc)
