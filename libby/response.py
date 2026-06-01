"""Turn a bamboo RPC envelope into a keyword-response dict.

``Libby.rpc`` returns a transport envelope, not the keyword response itself:

- ``{"status": "delivered", "resp": <payload|None>}``
- ``{"status": "timeout", ...}``
- ``{"status": "too_large", "mtu": ..., "size": ...}``

The keyword ``<payload>`` is ``{"ok": True, "value": ..., "units"?: ...}`` or
``{"ok": False, "error": "..."}``. ``unwrap`` raises on any failure; a caller
that prefers a dict (e.g. the CLI's table renderer) wraps it in its own
non-raising adapter.
"""
from __future__ import annotations

from typing import Any, Dict

from .errors import KeywordError, LibbyError, LibbyTimeout


def _prefix(name: str, message: str) -> str:
    return f"{name}: {message}" if name else message


def unwrap(name: str, envelope: Any) -> Dict[str, Any]:
    """Reduce an rpc envelope to its keyword-response dict, or raise.

    Raises ``LibbyTimeout`` (not delivered / no response), ``KeywordError``
    (daemon answered ``ok=False``), or ``LibbyError`` (malformed / oversized).
    """
    if not isinstance(envelope, dict):
        raise LibbyError(_prefix(name, f"unexpected response: {envelope!r}"))
    status = envelope.get("status")
    if status == "timeout":
        raise LibbyTimeout(_prefix(name, "request timed out"))
    if status == "too_large":
        raise LibbyError(_prefix(
            name, f"payload too large ({envelope.get('size')} > {envelope.get('mtu')})"
        ))
    # Delivered, or a non-enveloped transport that returned the response directly
    resp = envelope.get("resp", envelope) if "resp" in envelope else envelope
    if resp is None:
        raise LibbyTimeout(_prefix(name, "no response from peer"))
    if not isinstance(resp, dict):
        raise LibbyError(_prefix(name, f"unexpected response: {resp!r}"))
    if not resp.get("ok"):
        raise KeywordError(name, resp.get("error", "unknown error"))
    return resp
