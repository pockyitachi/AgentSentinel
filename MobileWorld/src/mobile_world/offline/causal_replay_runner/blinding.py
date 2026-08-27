"""Physically separable treatment-blind action-scoring projection."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    JsonValue,
    canonical_json_bytes,
    copy_json,
)
from mobile_world.offline.causal_replay_runner.contracts import (
    BLINDED_PACKET_SCHEMA_VERSION,
    BLINDING_MAPPING_SCHEMA_VERSION,
    REPLAY_SEEDS,
    BlindedActionPacket,
    BlindingMappingRecord,
    ReplayRunnerError,
)

_ALLOWED_DIAGNOSTIC_KEYS = frozenset({"parse_outcome", "action_count", "error_code"})
_ALLOWED_PARSER_OUTCOMES = frozenset(
    {
        "PARSED",
        "PARSE_ERROR",
        "REFUSAL",
        "EMPTY_RESPONSE",
        "NO_OP",
        "PROVIDER_ERROR",
        "NOT_RUN",
    }
)
_ALLOWED_ERROR_CODES = frozenset(
    {
        "MALFORMED_RESPONSE",
        "PARSER_FAILURE",
        "REFUSAL",
        "EMPTY_RESPONSE",
    }
)
_BLINDED_PACKET_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "blinded_packet_id",
        "normalized_action",
        "parser_outcome",
        "parser_diagnostics",
        "treatment_identity_present",
    }
)
_BLINDED_PACKET_ID_RE = re.compile(r"^g1blind-[0-9a-f]{24}$")
_RUN_ID_RE = re.compile(r"^g1run-[0-9a-f]{24}$")
_SCHEDULE_ID_RE = re.compile(r"^g1schedule-[0-9a-f]{24}$")
_IDENTITY_MARKERS = (
    "g1run-",
    "g1schedule-",
    "g1capsule-",
    "fake://",
    "mobileworld.g1.provider.",
    "responses/sha256/",
    "objects/sha256/",
)
_FORBIDDEN_ACTION_IDENTITY_KEYS = frozenset(
    {
        "arm",
        "arm_id",
        "arm_order_index",
        "arm_position",
        "block",
        "block_index",
        "capsule_id",
        "diff_sha256",
        "effective_arm",
        "endpoint_revision",
        "history_codec_id",
        "model_id",
        "order",
        "plan",
        "plan_id",
        "plan_set_sha256",
        "provider_codec_id",
        "repeat",
        "repeat_index",
        "replay_seed",
        "requested_arm",
        "run",
        "run_id",
        "schedule",
        "schedule_id",
        "seed",
        "selected_plan_sha256",
        "target_diff_sha256",
        "target_id",
        "unit_id",
    }
)


@dataclass(frozen=True)
class BlindingSeal:
    blinded_packet_id: str
    key_commitment_sha256: str
    mapping: BlindingMappingRecord
    confidential_values: tuple[str, ...]

    @property
    def confidential_mapping(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(self.mapping.to_dict())


def prepare_blinding(
    *,
    run_id: str,
    arm: ArmKind,
    schedule_id: str,
    secret_key: bytes,
    nonce: str,
    confidential_values: tuple[str, ...],
) -> BlindingSeal:
    if (
        len(secret_key) < 32
        or not nonce
        or _RUN_ID_RE.fullmatch(run_id) is None
        or _SCHEDULE_ID_RE.fullmatch(schedule_id) is None
        or not confidential_values
        or any(
            not isinstance(item, str) or not item  # type: ignore[redundant-expr]
            for item in confidential_values
        )
    ):
        raise ReplayRunnerError(
            "BLINDING_KEY_INVALID", "blinding needs a nonempty nonce and at least 32 key bytes"
        )
    key_commitment = hashlib.sha256(secret_key).hexdigest()
    sealed_values = tuple(sorted({run_id, arm.value, schedule_id, nonce, *confidential_values}))
    forbidden_value_set_sha256 = hashlib.sha256(
        canonical_json_bytes(list(sealed_values))
    ).hexdigest()
    mapping_subject: dict[str, JsonValue] = {
        "schema_version": BLINDING_MAPPING_SCHEMA_VERSION,
        "run_id": run_id,
        "arm_id": arm.value,
        "schedule_id": schedule_id,
        "blinding_nonce": nonce,
        "key_commitment_sha256": key_commitment,
        "forbidden_value_set_sha256": forbidden_value_set_sha256,
    }
    digest = hmac.new(
        secret_key,
        canonical_json_bytes(mapping_subject),
        hashlib.sha256,
    ).hexdigest()
    packet_id = f"g1blind-{digest[:24]}"
    return BlindingSeal(
        blinded_packet_id=packet_id,
        key_commitment_sha256=key_commitment,
        confidential_values=sealed_values,
        mapping=BlindingMappingRecord(
            blinded_packet_id=packet_id,
            run_id=run_id,
            arm=arm,
            schedule_id=schedule_id,
            blinding_nonce=nonce,
            key_commitment_sha256=key_commitment,
            forbidden_value_set_sha256=forbidden_value_set_sha256,
        ),
    )


def _make_blinded_packet(
    *,
    seal: BlindingSeal,
    normalized_action: dict[str, JsonValue] | None,
    parser_outcome: str,
    parser_diagnostics: dict[str, JsonValue],
) -> BlindedActionPacket:
    diagnostics = {
        key: copy_json(value)
        for key, value in parser_diagnostics.items()
        if key in _ALLOWED_DIAGNOSTIC_KEYS
    }
    packet = BlindedActionPacket(
        blinded_packet_id=seal.blinded_packet_id,
        _normalized_action_json=canonical_json_bytes(
            None
            if normalized_action is None
            else cast(dict[str, JsonValue], copy_json(normalized_action))
        ),
        parser_outcome=parser_outcome,
        _parser_diagnostics_json=canonical_json_bytes(diagnostics),
        _confidential_values=seal.confidential_values,
    )
    validate_blinded_packet(
        packet.to_dict(),
        confidential_values=seal.confidential_values,
    )
    return packet


def validate_blinded_packet(
    value: dict[str, JsonValue], *, confidential_values: tuple[str, ...] = ()
) -> None:
    if (
        set(value) != _BLINDED_PACKET_KEYS
        or value.get("schema_version") != BLINDED_PACKET_SCHEMA_VERSION
        or value.get("record_type") != "g1_blinded_action_packet"
        or not isinstance(value.get("blinded_packet_id"), str)
        or _BLINDED_PACKET_ID_RE.fullmatch(cast(str, value["blinded_packet_id"])) is None
        or (
            value.get("normalized_action") is not None
            and not isinstance(value.get("normalized_action"), dict)
        )
        or value.get("parser_outcome") not in _ALLOWED_PARSER_OUTCOMES
        or not isinstance(value.get("parser_diagnostics"), dict)
        or type(value.get("treatment_identity_present")) is not bool
        or value.get("treatment_identity_present") is not False
    ):
        raise ReplayRunnerError(
            "BLINDED_PACKET_INVALID", "blind packet envelope is invalid or not fail-closed"
        )
    diagnostics = cast(dict[str, JsonValue], value["parser_diagnostics"])
    if not set(diagnostics).issubset(_ALLOWED_DIAGNOSTIC_KEYS):
        raise ReplayRunnerError(
            "BLINDED_PACKET_INVALID", "blind parser diagnostics contain an unknown field"
        )
    parse_outcome = diagnostics.get("parse_outcome")
    action_count = diagnostics.get("action_count")
    error_code = diagnostics.get("error_code")
    if "parse_outcome" in diagnostics and parse_outcome not in _ALLOWED_PARSER_OUTCOMES:
        raise ReplayRunnerError(
            "BLINDED_PACKET_INVALID", "blind parse outcome diagnostic is invalid"
        )
    if action_count is not None and (type(action_count) is not int or action_count < 0):
        raise ReplayRunnerError(
            "BLINDED_PACKET_INVALID", "blind action count diagnostic is invalid"
        )
    if error_code is not None and error_code not in _ALLOWED_ERROR_CODES:
        raise ReplayRunnerError("BLINDED_PACKET_INVALID", "blind error diagnostic is invalid")
    top_outcome = value["parser_outcome"]
    normalized_action = value.get("normalized_action")
    coherent = parse_outcome == top_outcome
    if top_outcome in {"PARSED", "NO_OP"}:
        coherent = (
            coherent
            and isinstance(normalized_action, dict)
            and action_count == 1
            and error_code is None
        )
    elif top_outcome == "PARSE_ERROR":
        coherent = (
            coherent
            and normalized_action is None
            and action_count == 0
            and error_code in {"MALFORMED_RESPONSE", "PARSER_FAILURE"}
        )
    elif top_outcome == "REFUSAL":
        coherent = (
            coherent and normalized_action is None and action_count == 0 and error_code == "REFUSAL"
        )
    elif top_outcome == "EMPTY_RESPONSE":
        coherent = (
            coherent
            and normalized_action is None
            and action_count == 0
            and error_code == "EMPTY_RESPONSE"
        )
    else:
        coherent = (
            coherent
            and normalized_action is None
            and action_count in {None, 0}
            and error_code is None
        )
    if not coherent:
        raise ReplayRunnerError(
            "BLINDED_PACKET_INVALID",
            "blind parser outcome, diagnostics, and action are inconsistent",
        )
    arm_names = {arm.value.casefold() for arm in ArmKind}
    forbidden_exact = {item.casefold() for item in confidential_values if item} | {
        str(seed) for seed in REPLAY_SEEDS
    }

    def reject_identity_string(item: str, path: str) -> None:
        folded = item.casefold()
        exact_identity = folded in forbidden_exact or folded in arm_names
        embedded_identity = (
            any(marker.casefold() in folded for marker in _IDENTITY_MARKERS)
            or any(secret in folded for secret in forbidden_exact)
            or any(arm_name in folded for arm_name in arm_names)
        )
        if exact_identity or embedded_identity:
            raise ReplayRunnerError(
                "BLINDED_PACKET_LEAKAGE",
                "treatment identity appears in scorer-visible content",
                json_path=path,
            )

    def visit(item: JsonValue, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.casefold() in _FORBIDDEN_ACTION_IDENTITY_KEYS:
                    raise ReplayRunnerError(
                        "BLINDED_PACKET_LEAKAGE",
                        "treatment identity key appears in scorer-visible content",
                        json_path=f"{path}/{key}",
                    )
                reject_identity_string(key, f"{path}/{key}")
                visit(child, f"{path}/{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}/{index}")
        elif type(item) in {int, float} and item in REPLAY_SEEDS:
            raise ReplayRunnerError(
                "BLINDED_PACKET_LEAKAGE",
                "preregistered replay seed appears in scorer-visible content",
                json_path=path,
            )
        elif isinstance(item, str):
            reject_identity_string(item, path)

    # The action object is model output and its key names must remain unchanged;
    # only injected identity values are forbidden. Diagnostics are already
    # projected through a closed allowlist above.
    visit(
        value.get("normalized_action"),
        "/normalized_action",
    )
    visit(
        value.get("parser_outcome"),
        "/parser_outcome",
    )
    visit(
        value.get("parser_diagnostics"),
        "/parser_diagnostics",
    )


def order_blinded_packets(
    packets: tuple[BlindedActionPacket, ...], *, presentation_nonce: str
) -> tuple[BlindedActionPacket, ...]:
    """Order scorer packets without exposing the preregistered arm schedule."""

    if not presentation_nonce or len({item.blinded_packet_id for item in packets}) != len(packets):
        raise ReplayRunnerError(
            "BLINDED_PRESENTATION_INVALID",
            "presentation needs a nonempty nonce and unique opaque packet IDs",
        )
    union_confidential_values = tuple(
        sorted({value for packet in packets for value in packet._confidential_values})
    )
    for packet in packets:
        validate_blinded_packet(
            packet.to_dict(),
            confidential_values=union_confidential_values,
        )
    return tuple(
        sorted(
            packets,
            key=lambda item: hashlib.sha256(
                (
                    f"mobileworld-g1-scorer-order-v1|{presentation_nonce}|{item.blinded_packet_id}"
                ).encode()
            ).digest(),
        )
    )
