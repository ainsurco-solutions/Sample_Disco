from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)

active = False
unavailable_reason: str | None = None

def inject() -> bool:
    global active, unavailable_reason

    if active:
        return True

    try:
        import truststore
    except ImportError:
        unavailable_reason = "truststore is not installed"
        _LOG.debug("truststore unavailable: %s", unavailable_reason)
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        unavailable_reason = f"{type(exc).__name__}: {exc}"
        _LOG.warning("Could not use the OS certificate store: %s", unavailable_reason)
        return False

    active = True
    _LOG.debug("TLS verification is using the OS certificate store")
    return True
