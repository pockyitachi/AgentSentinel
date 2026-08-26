"""Shared policy for distinguishing configured secrets from local placeholders."""

from __future__ import annotations


def is_placeholder_credential(value: str | bytes) -> bool:
    """Return whether ``value`` is MobileWorld's non-secret local API-key marker.

    MobileWorld agents use ``empty`` when an OpenAI-compatible local endpoint
    does not require authentication.  CLI invocations have historically used
    both ``empty`` and ``EMPTY``.  Treat only that exact marker,
    case-insensitively, as a placeholder; all other non-empty values remain
    configured secrets and keep the collector's fail-closed protection.
    """

    if isinstance(value, str):
        return value.casefold() == "empty"
    if isinstance(value, bytes):
        return value.lower() == b"empty"
    raise TypeError("credential values must be strings or bytes")


__all__ = ["is_placeholder_credential"]
