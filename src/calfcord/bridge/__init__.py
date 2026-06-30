"""Discord ↔ calfkit bridge.

.. note::
   This package is mid-migration to the calfkit 0.12 caller surface (the pure
   ``Client`` bridge). The eager re-exports of the embedded-Worker era
   (``gateway``/``ingress``/``outbox``/``pending_wires``/``registry``) are being
   removed as those modules are rewritten or deleted; the final convenience
   surface is rebuilt once the gateway rewrite lands. Until then, import the
   bridge submodules directly (``from calfcord.bridge.history import …``) so the
   package stays importable while the embedded-Worker modules are inconsistent.
"""

from calfcord.bridge.wire import WireAuthor, WireMessage

__all__ = [
    "WireAuthor",
    "WireMessage",
]
