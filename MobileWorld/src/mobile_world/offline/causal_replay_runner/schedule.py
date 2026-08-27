"""Exact deterministic G1.1 arm schedule; no RNG or wall-clock inputs."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import cast

from mobile_world.offline.causal_replay.contracts import ArmKind, JsonValue, canonical_sha256
from mobile_world.offline.causal_replay_runner.contracts import (
    ARM_ORDER_SALT,
    CLEAN_ARMS,
    REPEATS,
    REPLAY_SEEDS,
    STRICT_ARMS,
    InvocationPlan,
    ReplayRunnerError,
    ScheduleEntry,
    UnitKind,
)

_CASE_UNIT_ID_RE = re.compile(r"^g1case-[0-9a-f]{24}$")
_CONTROL_UNIT_ID_RE = re.compile(r"^g1control-[0-9a-f]{24}$")
_MODEL_IDS = frozenset({"qwen3vl_8b", "mai_ui_8b"})


def _stable_id(prefix: str, value: JsonValue) -> str:
    return f"{prefix}-{canonical_sha256(value)[:24]}"


def arm_order_for_block(
    *, unit_kind: UnitKind, unit_id: str, model_id: str, block_zero_index: int
) -> tuple[ArmKind, ...]:
    if isinstance(block_zero_index, bool) or not isinstance(block_zero_index, int):
        raise ReplayRunnerError("INVALID_BLOCK_INDEX", "block index must be an integer")
    if block_zero_index < 0 or block_zero_index >= len(REPLAY_SEEDS) * len(REPEATS):
        raise ReplayRunnerError("INVALID_BLOCK_INDEX", "block index is outside the six blocks")
    if not unit_id or not model_id or "|" in unit_id or "|" in model_id:
        raise ReplayRunnerError("INVALID_SCHEDULE_IDENTITY", "schedule identity is invalid")
    base = STRICT_ARMS if unit_kind is UnitKind.STRICT_MHR else CLEAN_ARMS
    input_bytes = f"{ARM_ORDER_SALT}|{model_id}|{unit_id}".encode()
    digest = hashlib.sha256(input_bytes).digest()
    initial_rotation = digest[0] % len(base)
    direction = 1 if digest[1] % 2 == 0 else -1
    return tuple(
        base[(position + initial_rotation + direction * block_zero_index) % len(base)]
        for position in range(len(base))
    )


def schedule_for_unit(
    *, unit_kind: UnitKind, unit_id: str, model_id: str
) -> tuple[ScheduleEntry, ...]:
    unit_pattern = _CASE_UNIT_ID_RE if unit_kind is UnitKind.STRICT_MHR else _CONTROL_UNIT_ID_RE
    if unit_pattern.fullmatch(unit_id) is None or model_id not in _MODEL_IDS:
        raise ReplayRunnerError(
            "INVALID_SCHEDULE_IDENTITY",
            "schedule requires the frozen unit-kind ID pattern and model catalog",
        )
    input_text = f"{ARM_ORDER_SALT}|{model_id}|{unit_id}"
    input_sha = hashlib.sha256(input_text.encode()).hexdigest()
    entries: list[ScheduleEntry] = []
    block_zero_index = 0
    for seed in REPLAY_SEEDS:
        for repeat in REPEATS:
            order = arm_order_for_block(
                unit_kind=unit_kind,
                unit_id=unit_id,
                model_id=model_id,
                block_zero_index=block_zero_index,
            )
            order_sha = canonical_sha256([arm.value for arm in order])
            for position, arm in enumerate(order):
                schedule_subject: dict[str, JsonValue] = {
                    "protocol_version": "mobileworld.g1.causal-replay/protocol-v1",
                    "salt": ARM_ORDER_SALT,
                    "unit_kind": unit_kind.value,
                    "unit_id": unit_id,
                    "model_id": model_id,
                    "block_index": block_zero_index + 1,
                    "repeat_index": repeat,
                    "replay_seed": seed,
                    "arm_order_index": position,
                    "arm_id": arm.value,
                    "block_arm_order_sha256": order_sha,
                }
                entries.append(
                    ScheduleEntry(
                        unit_kind=unit_kind,
                        unit_id=unit_id,
                        model_id=model_id,
                        block_index=block_zero_index + 1,
                        repeat_index=repeat,
                        replay_seed=seed,
                        arm_order_index=position,
                        arm=arm,
                        block_arm_order=order,
                        arm_order_input_sha256=input_sha,
                        block_arm_order_sha256=order_sha,
                        schedule_id=_stable_id("g1schedule", schedule_subject),
                    )
                )
            block_zero_index += 1
    validate_schedule(entries)
    return tuple(entries)


def validate_schedule(entries: list[ScheduleEntry] | tuple[ScheduleEntry, ...]) -> None:
    if not entries:
        raise ReplayRunnerError("EMPTY_SCHEDULE", "unit schedule is empty")
    first = entries[0]
    expected_arms = STRICT_ARMS if first.unit_kind is UnitKind.STRICT_MHR else CLEAN_ARMS
    expected_count = 6 * len(expected_arms)
    if len(entries) != expected_count:
        raise ReplayRunnerError("SCHEDULE_COUNT_MISMATCH", "unit schedule has the wrong size")
    grouped: defaultdict[int, list[ScheduleEntry]] = defaultdict(list)
    for entry in entries:
        if (
            entry.unit_kind is not first.unit_kind
            or entry.unit_id != first.unit_id
            or entry.model_id != first.model_id
            or type(entry.block_index) is not int
            or type(entry.repeat_index) is not int
            or type(entry.replay_seed) is not int
            or type(entry.arm_order_index) is not int
        ):
            raise ReplayRunnerError("SCHEDULE_IDENTITY_DRIFT", "schedule mixes paired units")
        grouped[entry.block_index].append(entry)
    if set(grouped) != set(range(1, 7)):
        raise ReplayRunnerError("SCHEDULE_BLOCK_MISMATCH", "schedule must have six blocks")
    input_sha = hashlib.sha256(
        f"{ARM_ORDER_SALT}|{first.model_id}|{first.unit_id}".encode()
    ).hexdigest()
    position_counts: dict[ArmKind, Counter[int]] = {arm: Counter() for arm in expected_arms}
    for block_index, block in sorted(grouped.items()):
        ordered = sorted(block, key=lambda item: item.arm_order_index)
        expected_order = arm_order_for_block(
            unit_kind=first.unit_kind,
            unit_id=first.unit_id,
            model_id=first.model_id,
            block_zero_index=block_index - 1,
        )
        expected_order_sha = canonical_sha256([arm.value for arm in expected_order])
        if [item.arm_order_index for item in ordered] != list(range(len(expected_arms))):
            raise ReplayRunnerError("SCHEDULE_ORDER_MISMATCH", "block positions are not canonical")
        if tuple(item.arm for item in ordered) != expected_order:
            raise ReplayRunnerError("SCHEDULE_ORDER_MISMATCH", "block violates locked rotation")
        if {item.replay_seed for item in ordered} != {REPLAY_SEEDS[(block_index - 1) // 2]}:
            raise ReplayRunnerError("SCHEDULE_SEED_MISMATCH", "block has the wrong seed")
        if {item.repeat_index for item in ordered} != {REPEATS[(block_index - 1) % 2]}:
            raise ReplayRunnerError("SCHEDULE_REPEAT_MISMATCH", "block has the wrong repeat")
        for item in ordered:
            schedule_subject: dict[str, JsonValue] = {
                "protocol_version": "mobileworld.g1.causal-replay/protocol-v1",
                "salt": ARM_ORDER_SALT,
                "unit_kind": item.unit_kind.value,
                "unit_id": item.unit_id,
                "model_id": item.model_id,
                "block_index": item.block_index,
                "repeat_index": item.repeat_index,
                "replay_seed": item.replay_seed,
                "arm_order_index": item.arm_order_index,
                "arm_id": item.arm.value,
                "block_arm_order_sha256": expected_order_sha,
            }
            if (
                item.block_arm_order != expected_order
                or item.arm_order_input_sha256 != input_sha
                or item.block_arm_order_sha256 != expected_order_sha
                or item.schedule_id != _stable_id("g1schedule", schedule_subject)
            ):
                raise ReplayRunnerError(
                    "SCHEDULE_BINDING_MISMATCH",
                    "schedule IDs or frozen order hashes do not recompute",
                )
            position_counts[item.arm][item.arm_order_index] += 1
    for arm, counts in position_counts.items():
        values = [counts[position] for position in range(len(expected_arms))]
        if max(values) - min(values) > 1:
            raise ReplayRunnerError(
                "SCHEDULE_POSITION_IMBALANCE", f"{arm.value} positions differ by more than one"
            )
    if first.unit_kind is UnitKind.CLEAN_CONTROL:
        first_position = Counter(
            min(block, key=lambda item: item.arm_order_index).arm for block in grouped.values()
        )
        if any(first_position[arm] != 3 for arm in CLEAN_ARMS):
            raise ReplayRunnerError(
                "SCHEDULE_POSITION_IMBALANCE", "clean-control first position must be 3/3"
            )


def validate_schedule_block(entries: tuple[ScheduleEntry, ...]) -> None:
    """Bind a single preflight block to the exact frozen six-block schedule."""

    if not entries:
        raise ReplayRunnerError("EMPTY_SCHEDULE_BLOCK", "preflight block is empty")
    first = entries[0]
    expected = tuple(
        item
        for item in schedule_for_unit(
            unit_kind=first.unit_kind,
            unit_id=first.unit_id,
            model_id=first.model_id,
        )
        if item.block_index == first.block_index
    )
    if tuple(item.to_dict() for item in entries) != tuple(item.to_dict() for item in expected):
        raise ReplayRunnerError(
            "SCHEDULE_BLOCK_BINDING_MISMATCH",
            "preflight block differs from the exact locked schedule",
        )


def validate_schedule_entry(entry: ScheduleEntry) -> None:
    """Rebind one execution entry to the exact canonical unit schedule."""

    expected = tuple(
        item
        for item in schedule_for_unit(
            unit_kind=entry.unit_kind,
            unit_id=entry.unit_id,
            model_id=entry.model_id,
        )
        if item.block_index == entry.block_index and item.arm_order_index == entry.arm_order_index
    )
    if len(expected) != 1 or entry.to_dict() != expected[0].to_dict():
        raise ReplayRunnerError(
            "SCHEDULE_ENTRY_BINDING_MISMATCH",
            "execution schedule entry differs from the exact locked schedule",
        )


def logical_run_id(
    entry: ScheduleEntry,
    *,
    capsule_body_sha256: str,
    plan_set_sha256: str,
    selected_plan_sha256: str,
    history_codec_sha256: str,
    provider_codec_sha256: str,
    parser_binding_sha256: str,
    model_binding_sha256: str,
    provider_binding_sha256: str,
    model_parameters_sha256: str,
    code_sha256: str,
    config_sha256: str,
) -> str:
    subject: dict[str, JsonValue] = {
        "protocol_version": "mobileworld.g1.causal-replay/protocol-v1",
        "unit_id": entry.unit_id,
        "unit_kind": entry.unit_kind.value,
        "model_id": entry.model_id,
        "capsule_body_sha256": capsule_body_sha256,
        "plan_set_sha256": plan_set_sha256,
        "selected_plan_sha256": selected_plan_sha256,
        "schedule": entry.to_dict(),
        "history_codec_sha256": history_codec_sha256,
        "provider_codec_sha256": provider_codec_sha256,
        "parser_binding_sha256": parser_binding_sha256,
        "model_binding_sha256": model_binding_sha256,
        "provider_binding_sha256": provider_binding_sha256,
        "model_parameters_sha256": model_parameters_sha256,
        "code_sha256": code_sha256,
        "config_sha256": config_sha256,
    }
    return _stable_id("g1run", subject)


def validate_invocation_plan_identity(plan: InvocationPlan) -> None:
    """Recompute the content-addressed run identity before any durable write."""

    validate_schedule_entry(plan.schedule)
    expected_run_id = logical_run_id(
        plan.schedule,
        capsule_body_sha256=cast(str, plan.capsule_binding["capsule_body_sha256"]),
        plan_set_sha256=plan.plan_set_sha256,
        selected_plan_sha256=plan.selected_plan_sha256,
        history_codec_sha256=plan.history_codec_sha256,
        provider_codec_sha256=plan.provider_codec_sha256,
        parser_binding_sha256=plan.parser_binding_sha256,
        model_binding_sha256=plan.model_binding_sha256,
        provider_binding_sha256=plan.provider_binding_sha256,
        model_parameters_sha256=plan.model_parameters_sha256,
        code_sha256=plan.code_sha256,
        config_sha256=plan.config_sha256,
    )
    if plan.run_id != expected_run_id:
        raise ReplayRunnerError(
            "INVOCATION_PLAN_BINDING_MISMATCH",
            "logical run ID does not match the complete invocation-plan identity",
        )
