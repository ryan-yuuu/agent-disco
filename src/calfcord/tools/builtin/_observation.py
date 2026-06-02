"""Shared helper for rendering ``openhands`` :class:`Observation` objects
as a single LLM-facing string.

Every builtin tool that delegates to an openhands executor receives an
:class:`Observation` with two fields we care about for the calfkit
``return type=str`` contract:

* ``is_error: bool`` — true when the upstream tool errored (file not
  found, multi-occurrence on str_replace, permission denied, etc.).
* ``content: Sequence[TextContent | ImageContent]`` — the human-readable
  body, possibly multi-part. Builtin tools today never emit
  :class:`ImageContent`, so we walk for ``.text`` only.

Without this helper, a tool wrapper that simply concatenates
``content[*].text`` returns text indistinguishable from a successful
call. The LLM cannot reliably detect failure — and the existing
``private_chat`` convention is "errors are returned as strings starting
with ``error:``" so the LLM can adapt without triggering retry-with-
feedback.

:func:`flatten_observation_text` enforces that convention by checking
``is_error`` and prefixing accordingly. Use this in every wrapper that
delegates to an openhands executor.
"""

from __future__ import annotations

_ERROR_PREFIX = "error: "
"""Mirrors the convention in
:mod:`calfcord.tools.builtin.private_chat` — recoverable failures
return strings starting with ``error:`` so the calling LLM can read the
discriminator without parsing structured fields."""


def flatten_observation_text(obs: object) -> str:
    """Render an openhands :class:`Observation` as a single LLM-facing string.

    Args:
        obs: An openhands ``Observation`` (Pydantic model with ``content``
            and ``is_error`` fields). Typed as ``object`` to keep this
            module free of an openhands-sdk import at the top level —
            the runtime field-access via ``getattr`` works against any
            of openhands' concrete observation types.

    Returns:
        The concatenated ``.text`` of every ``TextContent`` in
        ``obs.content``, joined by blank lines. When ``obs.is_error`` is
        true, the result is prefixed with ``"error: "`` so the LLM can
        distinguish failure from success. Empty content (rare; can
        happen for no-op upstream paths) becomes ``"(no output)"``.
    """
    parts: list[str] = []
    for item in getattr(obs, "content", ()) or ():
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    body = "\n\n".join(parts) if parts else "(no output)"
    if getattr(obs, "is_error", False):
        return _ERROR_PREFIX + body
    return body
