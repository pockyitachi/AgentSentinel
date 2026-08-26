import re

import pytest

from mobile_world.runtime.audit.ids import is_valid_ulid, new_ulid, ulid_timestamp_ms


def test_deterministic_ulid_round_trip() -> None:
    value = new_ulid(timestamp_ms=1_725_000_123_456, randomness=bytes(range(10)))

    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", value)
    assert is_valid_ulid(value)
    assert ulid_timestamp_ms(value) == 1_725_000_123_456


def test_zero_ulid_has_canonical_encoding() -> None:
    assert new_ulid(timestamp_ms=0, randomness=b"\x00" * 10) == "0" * 26


def test_generated_ulids_are_unique() -> None:
    values = {new_ulid(timestamp_ms=1234) for _ in range(2_000)}

    assert len(values) == 2_000
    assert all(is_valid_ulid(value) for value in values)


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"timestamp_ms": -1}, ValueError),
        ({"timestamp_ms": 1 << 48}, ValueError),
        ({"timestamp_ms": True}, TypeError),
        ({"randomness": b"short"}, ValueError),
        ({"randomness": bytearray(10)}, TypeError),
    ],
)
def test_ulid_rejects_invalid_components(kwargs: dict, exception: type[Exception]) -> None:
    with pytest.raises(exception):
        new_ulid(**kwargs)


def test_validation_rejects_noncanonical_values() -> None:
    assert not is_valid_ulid("0" * 25)
    assert not is_valid_ulid("8" + "0" * 25)
    assert not is_valid_ulid("i" + "0" * 25)
    assert not is_valid_ulid(123)
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")
