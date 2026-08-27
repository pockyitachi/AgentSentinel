"""Fail-closed loader for active G1.3 ReplayCapsule runtime projections."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay_runner.contracts import (
    LoadedReplayCapsule,
    ReplayRunnerError,
    UnitKind,
)

ACTIVE_CAPSULE_SCHEMA = "mobileworld.g1.replay-capsule/v1.1"
ACTIVE_MANIFEST_SCHEMA = "mobileworld.g1.replay-capsule-manifest/v1.1"
ACTIVE_SCHEMA_GENERATION = "ACTIVE_V1_1"
ACTIVE_PUBLICATION_MANIFEST_SHA256 = (
    "8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402"
)
ACTIVE_CAPSULE_SET_SHA256 = "7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed"
ACTIVE_FILE_COUNT = 1600
ACTIVE_TOTAL_BYTE_COUNT = 116_169_862
PUBLICATION_STORE_ID = "G1_3_PUBLICATION"
REQUIRED_SOURCE_SAFETY_FALSE = (
    "execution_ready",
    "provider_invocation_allowed",
    "treatment_response_generation_allowed",
    "provider_invoked",
    "gpu_used",
    "gui_action_executed",
    "generated_action_executed",
    "raw_collector_mutated",
    "automatic_semantic_inference_performed",
    "runtime_sentinel_enabled",
)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _require(condition: bool, code: str, message: str, *, path: str | None = None) -> None:
    if not condition:
        raise ReplayRunnerError(code, message, json_path=path)


def _parse_canonical_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayRunnerError("CAPSULE_JSON_INVALID", f"{label} is not valid UTF-8 JSON") from exc
    _require(isinstance(value, dict), "CAPSULE_JSON_INVALID", f"{label} must be an object")
    canonical = canonical_json_bytes(cast(JsonValue, value))
    _require(
        data in {canonical, canonical + b"\n"},
        "CAPSULE_JSON_NONCANONICAL",
        f"{label} is not canonical JSON or one canonical JSONL record",
    )
    return cast(dict[str, Any], value)


def _safe_flat_name(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value), "CAPSULE_REF_INVALID", f"{label} is missing")
    assert isinstance(value, str)
    path = PurePosixPath(value)
    _require(
        not path.is_absolute() and len(path.parts) == 1 and path.parts[0] not in {"", ".", ".."},
        "CAPSULE_REF_INVALID",
        f"{label} must be one safe publication-local filename",
    )
    return value


def _read_root_file(root: Path, name: str) -> bytes:
    safe_name = _safe_flat_name(name, "publication reference")
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        raise ReplayRunnerError(
            "CAPSULE_ROOT_INVALID", "publication root cannot be opened"
        ) from exc
    try:
        try:
            file_fd = os.open(safe_name, file_flags, dir_fd=root_fd)
        except OSError as exc:
            raise ReplayRunnerError(
                "CAPSULE_REF_UNRESOLVED", "publication-local reference cannot be opened"
            ) from exc
        try:
            before = os.fstat(file_fd)
            _require(
                stat.S_ISREG(before.st_mode),
                "CAPSULE_REF_INVALID",
                "publication reference is not a regular file",
            )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_fd)
            _require(
                (before.st_dev, before.st_ino, before.st_size)
                == (after.st_dev, after.st_ino, after.st_size),
                "CAPSULE_REF_CHANGED",
                "publication reference changed while being read",
            )
            data = b"".join(chunks)
            _require(
                len(data) == before.st_size,
                "CAPSULE_REF_CHANGED",
                "publication reference length changed while being read",
            )
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(root_fd)


def _validate_directory_receipt(
    receipt: dict[str, Any], *, root: Path, manifest_sha256: str
) -> None:
    required_true = (
        "valid",
        "structural_valid",
        "formal_publication_valid",
        "source_bound_valid",
        "source_rebuild_performed",
        "source_rebuild_byte_identical",
        "exact_file_set",
        "regular_files_only",
        "zero_symlinks",
        "read_only",
    )
    for key in required_true:
        _require(
            type(receipt.get(key)) is bool and receipt[key] is True,
            "CAPSULE_SOURCE_BOUND_RECEIPT_REQUIRED",
            f"directory receipt {key} must be exact true",
            path=f"/directory_receipt/{key}",
        )
    _require(
        receipt.get("validation_scope") == "SOURCE_BOUND"
        and receipt.get("artifact_schema_generation") == ACTIVE_SCHEMA_GENERATION
        and receipt.get("capsule_schema_version") == ACTIVE_CAPSULE_SCHEMA
        and receipt.get("superseded_for_formal_g1") is False,
        "CAPSULE_SOURCE_BOUND_RECEIPT_REQUIRED",
        "runner accepts only active v1.1 SOURCE_BOUND formal publications",
    )
    _require(
        receipt.get("manifest_sha256") == manifest_sha256 == root.name,
        "CAPSULE_MANIFEST_BINDING_MISMATCH",
        "directory receipt, root name, and manifest digest differ",
    )
    _require(
        manifest_sha256 == ACTIVE_PUBLICATION_MANIFEST_SHA256
        and receipt.get("capsule_set_sha256") == ACTIVE_CAPSULE_SET_SHA256
        and receipt.get("file_count") == ACTIVE_FILE_COUNT
        and receipt.get("total_byte_count") == ACTIVE_TOTAL_BYTE_COUNT,
        "CAPSULE_PUBLICATION_NOT_PINNED",
        "directory receipt does not bind the active corrected G1.3 publication",
    )
    for key in (
        "provider_invoked",
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
        "execution_ready",
        "gpu_used",
        "gui_action_executed",
        "raw_collector_mutated",
    ):
        _require(
            type(receipt.get(key)) is bool and receipt[key] is False,
            "CAPSULE_AUTHORIZATION_GUARD_INVALID",
            f"directory receipt {key} must be exact false",
            path=f"/directory_receipt/{key}",
        )


def _read_content_ref(root: Path, ref: Any, label: str) -> bytes:
    _require(isinstance(ref, dict), "CAPSULE_REF_INVALID", f"{label} must be an object")
    assert isinstance(ref, dict)
    _require(
        ref.get("store_id") == PUBLICATION_STORE_ID,
        "CAPSULE_REF_INVALID",
        f"{label} must resolve inside the formal G1.3 publication",
    )
    name = _safe_flat_name(ref.get("relative_path"), label)
    data = _read_root_file(root, name)
    _require(
        type(ref.get("byte_count")) is int and ref["byte_count"] == len(data),
        "CAPSULE_REF_LENGTH_MISMATCH",
        f"{label} byte count differs",
    )
    _require(
        isinstance(ref.get("sha256"), str) and ref["sha256"] == _sha256(data),
        "CAPSULE_REF_HASH_MISMATCH",
        f"{label} digest differs",
    )
    return data


def load_replay_capsule(
    capsule_root: str | os.PathLike[str],
    *,
    unit_id: str,
    directory_receipt: dict[str, Any],
) -> LoadedReplayCapsule:
    """Load one active capsule and expose only its pre-cutoff runtime projection.

    The caller must first obtain a SOURCE_BOUND receipt from G1.3's directory
    validator.  This function never exposes curator or post-action roots to the
    replay runner.
    """

    supplied = Path(capsule_root)
    _require(not supplied.is_symlink(), "CAPSULE_ROOT_INVALID", "publication root is a symlink")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise ReplayRunnerError("CAPSULE_ROOT_INVALID", "publication root is unavailable") from exc
    _require(root.is_dir(), "CAPSULE_ROOT_INVALID", "publication root is not a directory")
    manifest_bytes = _read_root_file(root, "capsule_manifest.json")
    manifest_sha = _sha256(manifest_bytes)
    _require(
        manifest_sha == root.name,
        "CAPSULE_MANIFEST_BINDING_MISMATCH",
        "publication basename is not the manifest digest",
    )
    _validate_directory_receipt(directory_receipt, root=root, manifest_sha256=manifest_sha)
    manifest = _parse_canonical_object(manifest_bytes, "capsule manifest")
    _require(
        manifest.get("schema_version") == ACTIVE_MANIFEST_SCHEMA
        and manifest.get("publication_phase") == "FORMAL_PUBLICATION_READY",
        "CAPSULE_SCHEMA_GENERATION_UNSUPPORTED",
        "only the active formal v1.1 publication is runnable",
    )
    _require(
        manifest.get("capsule_set_sha256") == ACTIVE_CAPSULE_SET_SHA256
        and manifest.get("counts", {}).get("capsuled_count") == 190
        and manifest.get("counts", {}).get("excluded_count") == 0,
        "CAPSULE_PUBLICATION_NOT_PINNED",
        "manifest population or capsule-set binding differs from the active publication",
    )
    units = manifest.get("units")
    _require(isinstance(units, list), "CAPSULE_MANIFEST_INVALID", "manifest unit table is missing")
    assert isinstance(units, list)
    matches = [row for row in units if isinstance(row, dict) and row.get("unit_id") == unit_id]
    _require(
        len(matches) == 1,
        "CAPSULE_UNIT_UNRESOLVED",
        "unit must occur exactly once in the publication manifest",
    )
    unit_row = matches[0]
    _require(
        unit_row.get("disposition") == "CAPSULED",
        "CAPSULE_UNIT_EXCLUDED",
        "excluded unit has no runnable capsule",
    )
    capsule_ref = unit_row.get("capsule_ref")
    _require(isinstance(capsule_ref, dict), "CAPSULE_REF_INVALID", "capsule file ref is missing")
    assert isinstance(capsule_ref, dict)
    capsule_name = _safe_flat_name(capsule_ref.get("relative_path"), "capsule file")
    capsule_bytes = _read_root_file(root, capsule_name)
    capsule_file_sha = _sha256(capsule_bytes)
    _require(
        capsule_ref.get("sha256") == capsule_file_sha
        and capsule_ref.get("byte_count") == len(capsule_bytes),
        "CAPSULE_REF_HASH_MISMATCH",
        "capsule file differs from its manifest binding",
    )
    envelope = _parse_canonical_object(capsule_bytes, "replay capsule")
    _require(
        envelope.get("schema_version") == ACTIVE_CAPSULE_SCHEMA,
        "CAPSULE_SCHEMA_GENERATION_UNSUPPORTED",
        "capsule is not active v1.1",
    )
    body = envelope.get("capsule")
    _require(isinstance(body, dict), "CAPSULE_BODY_INVALID", "capsule body is missing")
    assert isinstance(body, dict)
    body_sha = canonical_sha256(cast(JsonValue, body))
    _require(
        body_sha == envelope.get("capsule_body_sha256") == unit_row.get("capsule_body_sha256"),
        "CAPSULE_BODY_HASH_MISMATCH",
        "capsule body does not match its envelope and manifest",
    )
    unit = body.get("unit")
    runtime = body.get("runtime")
    safety = body.get("safety")
    _require(
        isinstance(unit, dict) and isinstance(runtime, dict) and isinstance(safety, dict),
        "CAPSULE_BODY_INVALID",
        "capsule runtime binding is incomplete",
    )
    assert isinstance(unit, dict)
    assert isinstance(runtime, dict)
    assert isinstance(safety, dict)
    _require(
        unit.get("unit_id") == unit_id
        and unit.get("unit_kind") == unit_row.get("unit_kind")
        and unit.get("model_id") == unit_row.get("model_id")
        and unit.get("history_family") == unit_row.get("history_family"),
        "CAPSULE_UNIT_BINDING_MISMATCH",
        "capsule identity differs from the manifest row",
    )
    for key in REQUIRED_SOURCE_SAFETY_FALSE:
        _require(
            type(safety.get(key)) is bool and safety[key] is False,
            "CAPSULE_AUTHORIZATION_GUARD_INVALID",
            f"capsule safety field {key} must be exact false",
            path=f"/capsule/safety/{key}",
        )
    _require(
        type(safety.get("treatment_response_count")) is int
        and safety.get("treatment_response_count") == 0,
        "CAPSULE_AUTHORIZATION_GUARD_INVALID",
        "capsule treatment response count must remain zero",
    )
    model_visible = runtime.get("model_visible")
    non_history = runtime.get("non_history_envelope")
    treatment = runtime.get("treatment_surface")
    _require(
        isinstance(model_visible, dict)
        and isinstance(non_history, dict)
        and isinstance(treatment, dict),
        "CAPSULE_RUNTIME_INVALID",
        "capsule runtime roots are incomplete",
    )
    assert isinstance(model_visible, dict)
    assert isinstance(non_history, dict)
    assert isinstance(treatment, dict)
    semantic = model_visible.get("semantic_request")
    regions = model_visible.get("region_partition")
    replay_binding = non_history.get("replay_binding")
    restore = non_history.get("restore_descriptor")
    _require(
        isinstance(semantic, dict)
        and isinstance(regions, list)
        and isinstance(replay_binding, dict)
        and isinstance(restore, dict),
        "CAPSULE_RUNTIME_INVALID",
        "semantic request, regions, replay binding, or restore descriptor is missing",
    )
    assert isinstance(semantic, dict)
    assert isinstance(regions, list)
    assert isinstance(replay_binding, dict)
    assert isinstance(restore, dict)
    _require(
        restore.get("mode") == "SERIALIZED_REQUEST_ONLY"
        and restore.get("external_state_consulted") is False
        and restore.get("checkpoint_required") is False,
        "LIVE_EXTERNAL_STATE_NOT_RESTORED",
        "CPU G1.4 accepts only serialized-request-only capsules",
    )
    semantic_bytes = _read_content_ref(
        root, semantic.get("canonical_semantic_request_ref"), "semantic request"
    )
    semantic_request = _parse_canonical_object(semantic_bytes, "semantic request")
    semantic_sha = _sha256(semantic_bytes)
    _require(
        semantic_sha == semantic.get("canonical_semantic_request_sha256")
        and semantic_sha == canonical_sha256(cast(JsonValue, semantic_request)),
        "SEMANTIC_REQUEST_HASH_MISMATCH",
        "semantic request does not match capsule identity",
    )
    parser_binding = replay_binding.get("parser")
    provider_binding = replay_binding.get("provider")
    _require(
        isinstance(parser_binding, dict) and isinstance(provider_binding, dict),
        "CAPSULE_REPLAY_BINDING_INVALID",
        "parser or provider replay binding is missing",
    )
    assert isinstance(parser_binding, dict)
    assert isinstance(provider_binding, dict)
    parser_bytes = _read_content_ref(
        root, parser_binding.get("implementation_ref"), "parser descriptor"
    )
    parser_descriptor = _parse_canonical_object(parser_bytes, "parser descriptor")
    decoding_bytes = _read_content_ref(
        root, provider_binding.get("decoding_configuration_ref"), "decoding configuration"
    )
    decoding_configuration = _parse_canonical_object(decoding_bytes, "decoding configuration")
    _require(
        _sha256(parser_bytes) == parser_binding.get("implementation_sha256")
        and _sha256(decoding_bytes)
        == provider_binding.get("decoding_configuration_sha256")
        == non_history.get("provider_envelope_sha256"),
        "CAPSULE_REPLAY_BINDING_INVALID",
        "parser or decoding artifact digest differs from the replay binding",
    )
    semantic_without_messages = {
        key: copy_json(cast(JsonValue, value))
        for key, value in semantic_request.items()
        if key != "messages"
    }
    _require(
        decoding_configuration == semantic_without_messages,
        "CAPSULE_REPLAY_BINDING_INVALID",
        "decoding configuration is not the semantic request excluding messages",
    )
    return LoadedReplayCapsule(
        publication_manifest_sha256=manifest_sha,
        capsule_file_sha256=capsule_file_sha,
        capsule_body_sha256=body_sha,
        capsule_id=cast(str, body["capsule_id"]),
        unit_kind=UnitKind(cast(str, unit["unit_kind"])),
        unit_id=unit_id,
        model_id=cast(str, unit["model_id"]),
        history_family=cast(str, unit["history_family"]),
        semantic_request=copy_json(cast(JsonValue, semantic_request)),
        semantic_request_sha256=semantic_sha,
        region_partition=tuple(
            cast(dict[str, JsonValue], copy_json(cast(JsonValue, region))) for region in regions
        ),
        non_history_projection_sha256=cast(str, model_visible["non_history_projection_sha256"]),
        treatment_surface=cast(dict[str, JsonValue], copy_json(cast(JsonValue, treatment))),
        replay_binding=cast(dict[str, JsonValue], copy_json(cast(JsonValue, replay_binding))),
        restore_descriptor=cast(dict[str, JsonValue], copy_json(cast(JsonValue, restore))),
        parser_descriptor=cast(dict[str, JsonValue], copy_json(cast(JsonValue, parser_descriptor))),
        decoding_configuration=cast(
            dict[str, JsonValue], copy_json(cast(JsonValue, decoding_configuration))
        ),
        source_safety=cast(dict[str, JsonValue], copy_json(cast(JsonValue, safety))),
    )
