"""Dependency-free ULID generation for audit event identifiers.

ULIDs provide the 128 bits required by the audit contract while keeping the
timestamp prefix useful during manual inspection.  Ordering inside an audit
stream is still defined by ``seq``; callers must not use an ID as a substitute
for an event's causal links.
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {character: index for index, character in enumerate(_CROCKFORD_BASE32)}
_MAX_TIMESTAMP_MS = (1 << 48) - 1
_RANDOMNESS_BYTES = 10
_ULID_LENGTH = 26


def new_ulid(*, timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Return a standards-compatible 26-character ULID.

    ``timestamp_ms`` and ``randomness`` are injectable solely to make format
    and boundary tests deterministic.  Production callers should omit both.
    """

    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool):
        raise TypeError("timestamp_ms must be an integer")
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp_ms must fit in the ULID 48-bit timestamp field")

    if randomness is None:
        randomness = secrets.token_bytes(_RANDOMNESS_BYTES)
    if not isinstance(randomness, bytes):
        raise TypeError("randomness must be bytes")
    if len(randomness) != _RANDOMNESS_BYTES:
        raise ValueError("randomness must contain exactly 10 bytes")

    value = (timestamp_ms << 80) | int.from_bytes(randomness, byteorder="big")
    encoded = ["0"] * _ULID_LENGTH
    for index in range(_ULID_LENGTH - 1, -1, -1):
        encoded[index] = _CROCKFORD_BASE32[value & 0x1F]
        value >>= 5
    return "".join(encoded)


def is_valid_ulid(value: object) -> bool:
    """Return whether *value* is a canonical uppercase ULID string."""

    if not isinstance(value, str) or len(value) != _ULID_LENGTH:
        return False
    if any(character not in _DECODE for character in value):
        return False
    # A ULID encodes 128 bits in 130 base32 bits, so the first digit is 0..7.
    return _DECODE[value[0]] <= 7


def ulid_timestamp_ms(value: str) -> int:
    """Decode and return the millisecond timestamp embedded in *value*."""

    if not is_valid_ulid(value):
        raise ValueError("value is not a canonical ULID")
    decoded = 0
    for character in value:
        decoded = (decoded << 5) | _DECODE[character]
    return decoded >> 80


# A neutral alias keeps recorder code independent from the chosen v1 ID family.
new_id = new_ulid


__all__ = ["is_valid_ulid", "new_id", "new_ulid", "ulid_timestamp_ms"]
