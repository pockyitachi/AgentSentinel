from __future__ import annotations

import copy

import pytest

from mobile_world.offline.failure_link_prompt import (
    ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    INLINE_CARD_TRANSPORT_ENCODING,
    LARGE_CARD_ENCODING_THRESHOLD_BYTES,
    PROMPT_VERSION,
    build_review_prompt,
    decode_card_transport,
    prepare_card_for_prompt,
    select_card_transport_encoding,
)
from mobile_world.offline.motivation_review import canonical_json_bytes


def _large_repeated_card() -> dict[str, object]:
    repeated = {
        "action": "tap",
        "content": "same historical assistant action " * 80,
        "step": 7,
    }
    return {
        "padding": "p" * (LARGE_CARD_ENCODING_THRESHOLD_BYTES + 1),
        "trace": [
            {
                "assistant_exposures": [
                    repeated,
                    {"action": None, "step": 8},
                    {"content": "present", "nullable": None, "step": 9},
                    repeated,
                ]
                * 30
            },
            {"assistant_exposures": []},
        ],
    }


def test_assistant_exposure_transport_is_deterministic_lossless_and_nonmutating() -> None:
    card = _large_repeated_card()
    original = copy.deepcopy(card)
    original_bytes = canonical_json_bytes(card)

    assert select_card_transport_encoding(card) == ASSISTANT_EXPOSURES_TRANSPORT_ENCODING
    encoded, transport = prepare_card_for_prompt(
        card,
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    )
    encoded_again, transport_again = prepare_card_for_prompt(
        copy.deepcopy(card),
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    )

    assert card == original
    assert (encoded, transport) == (encoded_again, transport_again)
    assert decode_card_transport(encoded, transport) == original
    assert canonical_json_bytes(decode_card_transport(encoded, transport)) == original_bytes
    assert transport["original_card_canonical_byte_count"] == len(original_bytes)
    assert original_bytes.endswith(b"\n")
    assert len(transport["tables"]) == 3
    assert decode_card_transport(encoded, transport)["trace"][1]["assistant_exposures"] == []


def test_transport_is_independent_of_mapping_insertion_order() -> None:
    card = _large_repeated_card()
    reordered = {
        "trace": [
            {
                "assistant_exposures": [
                    {key: value for key, value in reversed(list(row.items()))}
                    for row in card["trace"][0]["assistant_exposures"]
                ]
            },
            {"assistant_exposures": []},
        ],
        "padding": card["padding"],
    }
    assert canonical_json_bytes(card) == canonical_json_bytes(reordered)
    assert prepare_card_for_prompt(
        card,
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    ) == prepare_card_for_prompt(
        reordered,
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    )


def test_inline_transport_is_explicit_and_byte_bound() -> None:
    card = {"assistant_exposures": [], "missing_is_not_null": None}
    encoded, transport = prepare_card_for_prompt(
        card,
        transport_encoding=INLINE_CARD_TRANSPORT_ENCODING,
    )
    assert encoded == card
    assert decode_card_transport(encoded, transport) == card
    assert transport["original_card_canonical_byte_count"] == len(canonical_json_bytes(card))
    prompt = build_review_prompt(
        phase="A",
        stage="PRIMARY",
        reviewer_id="reviewer",
        identity={"task_key": "model/task"},
        card=card,
        schema={"type": "object"},
        attachment_map=[],
        card_transport_encoding=INLINE_CARD_TRANSPORT_ENCODING,
    )
    assert f'"prompt_version":"{PROMPT_VERSION}"' in prompt
    assert f'"encoding":"{INLINE_CARD_TRANSPORT_ENCODING}"' in prompt


def test_large_card_without_exposure_rows_stays_inline() -> None:
    card = {
        "assistant_exposures": [],
        "padding": "x" * (LARGE_CARD_ENCODING_THRESHOLD_BYTES + 1),
    }
    assert select_card_transport_encoding(card) == INLINE_CARD_TRANSPORT_ENCODING
    with pytest.raises(ValueError, match="found no exposure rows"):
        prepare_card_for_prompt(
            card,
            transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda refs: refs.__setitem__(0, [True, 0]), "reference is malformed"),
        (lambda refs: refs.__setitem__(0, [999, 0]), "table reference is out of range"),
        (lambda refs: refs.__setitem__(0, [0, 999]), "row reference is out of range"),
    ],
)
def test_decoder_rejects_invalid_references(mutation: object, match: str) -> None:
    card = _large_repeated_card()
    encoded, transport = prepare_card_for_prompt(
        card,
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    )
    encoded = copy.deepcopy(encoded)
    refs = encoded["trace"][0]["assistant_exposures"]["$failure_link_assistant_exposure_refs"]
    mutation(refs)
    with pytest.raises(ValueError, match=match):
        decode_card_transport(encoded, transport)


def test_decoder_rejects_marker_and_transport_metadata_drift() -> None:
    card = _large_repeated_card()
    encoded, transport = prepare_card_for_prompt(
        card,
        transport_encoding=ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
    )
    malformed_card = copy.deepcopy(encoded)
    malformed_card["trace"][0]["assistant_exposures"] = {"wrong": []}
    with pytest.raises(ValueError, match="marker is malformed"):
        decode_card_transport(malformed_card, transport)

    malformed_transport = copy.deepcopy(transport)
    malformed_transport["unexpected"] = True
    with pytest.raises(ValueError, match="metadata is malformed"):
        decode_card_transport(encoded, malformed_transport)
