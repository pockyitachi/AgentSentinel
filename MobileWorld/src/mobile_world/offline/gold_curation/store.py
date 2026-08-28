"""Append-only, repository-external persistence for human annotation events."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.gold_curation.contracts import (
    ADJUDICATOR_ROLE,
    ANNOTATION_EVENT_SCHEMA_VERSION,
    CHANNELS,
    EVENT_KINDS,
    REVIEW_PROPOSAL_SCHEMA_VERSION,
    REVIEW_ROLES,
    CurationError,
    canonical_json_bytes,
    canonical_sha256,
    disagreement_fields,
    json_copy,
    material_projection,
    require,
    role_channel,
    validate_identity,
    validate_review_payload,
)
from mobile_world.offline.gold_curation.publication import (
    ACTIVE_G1_3_CAPSULE_SET_SHA256,
    ACTIVE_G1_3_MANIFEST_SHA256,
    ACTIVE_G1_3_PUBLICATION,
    CurationPublication,
)
from mobile_world.offline.gold_curation.schema_validation import validate_schema_record

WORKSPACE_MANIFEST_SCHEMA_VERSION: Final = "mobileworld.g1.gold-curation-workspace-manifest/v1"
REVIEWER_REGISTRY_SCHEMA_VERSION: Final = "mobileworld.g1.owner-reviewer-registry/v1"
MAX_EVENT_BYTES: Final = 2 * 1024 * 1024
FORMAL_SCHEMA_FILENAMES: Final = (
    "curation_input_manifest.schema.json",
    "curation_evidence.schema.json",
    "action_gold_bundle.schema.json",
    "transformation_plan.schema.json",
    "review_ledger.schema.json",
    "arm.schema.json",
    "admission.schema.json",
    "admission_validation.schema.json",
    "admission_seal.schema.json",
)
CODEC_GATE_SCHEMA_VERSION: Final = "mobileworld.g1.gold-curation-codec-gate/v1"
PREVIEW_API_SCHEMA_VERSION: Final = "mobileworld.g1.history-codec-preview-api/v1"
PREVIEW_API_SYMBOLS: Final = (
    "bind_human_record_spans",
    "rank_correction_candidates",
    "build_five_arm_preview",
    "build_clean_control_preview",
)
PREVIEW_MODEL_CONFIG_MANIFEST: Final = {
    "path": "mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
    "sha256": "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f",
}
PREVIEW_TOKENIZER_SPECS: Final = (
    {
        "model_id": "qwen3vl_8b",
        "history_family": "flat_progress",
        "tokenizer_id": "Qwen/Qwen3-VL-8B-Instruct",
        "tokenizer_revision": "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
        "tokenizer_binding_sha256": (
            "e97afc56a6ce6b1d0d78345efc2b27c9853e9251d1e2f2bb0ff60b9b99926efd"
        ),
    },
    {
        "model_id": "mai_ui_8b",
        "history_family": "raw_replay",
        "tokenizer_id": "Tongyi-MAI/MAI-UI-8B",
        "tokenizer_revision": "e00a0097abb9cc621cac5172d8c4809f0839c94e",
        "tokenizer_binding_sha256": (
            "dac3c7c7da1bcb043402cb3571a0867f98153c4fd3f3c0614153a6ea27518d23"
        ),
    },
)
CODEC_GATE_CHECKS: Final = {
    "selected_codec_count": 2,
    "codec_ids_distinct": True,
    "capabilities_sufficient": True,
    "conformance_receipts_valid": True,
    "fixture_only": False,
    "cpu_only": True,
    "provider_client_created": False,
    "provider_invoked": False,
    "external_network_used": False,
    "gpu_probed": False,
    "gpu_used": False,
    "model_loaded": False,
    "replay_executed": False,
}


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _ensure_root(root: Path, forbidden_roots: tuple[Path, ...]) -> Path:
    require(not root.is_symlink(), "ANNOTATION_ROOT_INVALID", "annotation root cannot be a symlink")
    parent = root.parent.resolve(strict=True)
    candidate = parent / root.name
    for forbidden in forbidden_roots:
        resolved = forbidden.resolve(strict=False)
        require(
            not _is_within(candidate, resolved) and not _is_within(resolved, candidate),
            "ANNOTATION_ROOT_FORBIDDEN",
            "annotation state must be outside source repositories and frozen publications",
        )
    if not candidate.exists():
        candidate.mkdir(mode=0o700)
    require(
        not candidate.is_symlink() and candidate.is_dir(),
        "ANNOTATION_ROOT_INVALID",
        "annotation root is unsafe",
    )
    mode = candidate.stat().st_mode
    require(stat.S_ISDIR(mode), "ANNOTATION_ROOT_INVALID", "annotation root is not a directory")
    root_stat = candidate.stat(follow_symlinks=False)
    require(
        root_stat.st_uid == os.geteuid() and root_stat.st_mode & 0o077 == 0,
        "ANNOTATION_ROOT_INVALID",
        "annotation root must be owner-restricted",
    )
    return candidate.resolve(strict=True)


def _read_regular(
    path: Path,
    *,
    missing_ok: bool = False,
    owner_restricted: bool = False,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CurationError(
            "ANNOTATION_STORE_UNRESOLVED", "annotation artifact is missing"
        ) from None
    except OSError as exc:
        raise CurationError(
            "ANNOTATION_STORE_INVALID", "annotation artifact cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode),
            "ANNOTATION_STORE_INVALID",
            "annotation artifact is not regular",
        )
        if owner_restricted:
            require(
                before.st_uid == os.geteuid()
                and before.st_nlink == 1
                and before.st_mode & 0o077 == 0,
                "ANNOTATION_STORE_INVALID",
                "annotation artifact ownership, link count, or mode is unsafe",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        require(
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
            )
            == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
            ),
            "ANNOTATION_STORE_CHANGED",
            "annotation artifact changed while being read",
        )
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        require(
            written > 0,
            "ANNOTATION_STORE_INVALID",
            "annotation artifact write made no progress",
        )
        view = view[written:]


def _ensure_child_directory(parent: Path, name: str) -> Path:
    require(
        bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name,
        "ANNOTATION_STORE_INVALID",
        "annotation child directory name is invalid",
    )
    path = parent / name
    if not path.exists():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CurationError(
                "ANNOTATION_STORE_INVALID", "annotation directory cannot be created safely"
            ) from exc
    require(
        not path.is_symlink() and path.is_dir(),
        "ANNOTATION_STORE_INVALID",
        "annotation directory is unsafe",
    )
    resolved = path.resolve(strict=True)
    directory_stat = path.stat(follow_symlinks=False)
    require(
        directory_stat.st_uid == os.geteuid() and directory_stat.st_mode & 0o077 == 0,
        "ANNOTATION_STORE_INVALID",
        "annotation directory must be owner-restricted",
    )
    require(
        _is_within(resolved, parent.resolve(strict=True)),
        "ANNOTATION_STORE_INVALID",
        "annotation directory escapes its parent",
    )
    return resolved


def _write_once_regular(path: Path, data: bytes) -> None:
    existing = _read_regular(path, missing_ok=True, owner_restricted=True)
    if existing is not None:
        require(
            existing == data,
            "ANNOTATION_STORE_COLLISION",
            "write-once annotation artifact differs",
        )
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_regular(path, owner_restricted=True)
        require(
            existing == data,
            "ANNOTATION_STORE_COLLISION",
            "write-once annotation artifact collision",
        )
        return
    except OSError as exc:
        raise CurationError(
            "ANNOTATION_STORE_INVALID", "annotation artifact cannot be created safely"
        ) from exc
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _formal_schema_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[5] / "mobileworld_audit_handoff" / "schemas" / "g1"
    result: dict[str, str] = {}
    for filename in FORMAL_SCHEMA_FILENAMES:
        data = _read_regular(root / filename)
        assert data is not None
        result[filename] = hashlib.sha256(data).hexdigest()
    return result


def _read_repo_binding(repository_root: Path, reference: Mapping[str, Any]) -> bytes:
    relative = reference.get("path")
    require(
        isinstance(relative, str)
        and relative == relative.strip()
        and "\\" not in relative
        and "\x00" not in relative,
        "CODEC_GATE_INVALID",
        "G1.5 publication reference path is invalid",
    )
    pure = PurePosixPath(cast(str, relative))
    require(
        not pure.is_absolute()
        and bool(pure.parts)
        and all(part not in {"", ".", ".."} for part in pure.parts),
        "CODEC_GATE_INVALID",
        "G1.5 publication reference path escapes the repository",
    )
    current = repository_root
    for part in pure.parts:
        current = current / part
        require(
            not current.is_symlink(),
            "CODEC_GATE_INVALID",
            "G1.5 publication reference traverses a symlink",
        )
    data = _read_regular(current)
    assert data is not None
    expected = reference.get("sha256", reference.get("file_sha256"))
    require(
        isinstance(expected, str) and hashlib.sha256(data).hexdigest() == expected,
        "CODEC_GATE_INVALID",
        "G1.5 publication reference digest differs",
    )
    return data


def _parse_codec_gate_json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("CODEC_GATE_INVALID", f"{description} is invalid JSON") from exc
    require(
        isinstance(value, dict),
        "CODEC_GATE_INVALID",
        f"{description} must be a JSON object",
    )
    return cast(dict[str, Any], value)


def _validate_preview_output_schema(schema_data: bytes) -> None:
    schema = _parse_codec_gate_json_object(schema_data, "G1.5 preview output schema")
    require(
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        and schema.get("$id")
        == "https://agentsentinel.local/schemas/g1_5/history_codec_preview.schema.json"
        and schema.get("type") == "object"
        and schema.get("additionalProperties") is False,
        "CODEC_GATE_INVALID",
        "G1.5 preview output schema identity or closed-root contract differs",
    )
    definitions = schema.get("$defs")
    preview_arm = definitions.get("previewArm") if isinstance(definitions, dict) else None
    preview_arm_properties = (
        preview_arm.get("properties") if isinstance(preview_arm, dict) else None
    )
    require(
        isinstance(preview_arm_properties, dict)
        and "rendered_request" not in preview_arm_properties,
        "CODEC_GATE_INVALID",
        "G1.5 preview output schema exposes a full rendered request",
    )
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise CurationError(
            "CODEC_GATE_INVALID", "G1.5 preview output schema fails meta-validation"
        ) from exc


def _validate_preview_implementation(data: bytes, symbols: tuple[str, ...]) -> None:
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise CurationError(
            "CODEC_GATE_INVALID", "G1.5 preview implementation is not valid UTF-8 Python"
        ) from exc
    defined_symbols = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    require(
        set(symbols).issubset(defined_symbols),
        "CODEC_GATE_INVALID",
        "G1.5 preview implementation is missing a published entrypoint",
    )


def _validate_preview_api(publication: Mapping[str, Any], repository_root: Path) -> dict[str, Any]:
    preview_raw = publication.get("preview_api")
    require(
        isinstance(preview_raw, dict)
        and set(preview_raw)
        == {
            "schema_version",
            "implementation",
            "dependencies",
            "output_schema",
            "input_contract",
            "supported_plan_set_profiles",
            "outputs",
            "pinned_tokenizers",
            "tokenizer_policy",
        },
        "CODEC_GATE_INVALID",
        "G1.5 preview API binding is missing or not closed",
    )
    preview = cast(dict[str, Any], preview_raw)
    require(
        preview.get("schema_version") == PREVIEW_API_SCHEMA_VERSION
        and preview.get("input_contract")
        == "EXACT_G1_3_SOURCE_RECORDS_PLUS_EXPLICIT_G1_6_HUMAN_SELECTIONS"
        and preview.get("supported_plan_set_profiles") == ["G1_STRICT_MHR", "G1_CLEAN_CONTROL"],
        "CODEC_GATE_INVALID",
        "G1.5 preview API identity, input contract, or plan profiles differ",
    )

    implementation_raw = preview.get("implementation")
    require(
        isinstance(implementation_raw, dict)
        and set(implementation_raw) == {"path", "sha256", "symbols"}
        and implementation_raw.get("path")
        == "MobileWorld/src/mobile_world/offline/g1_history_codecs/preview.py"
        and implementation_raw.get("symbols") == list(PREVIEW_API_SYMBOLS),
        "CODEC_GATE_INVALID",
        "G1.5 preview implementation binding differs",
    )
    implementation = cast(dict[str, Any], implementation_raw)
    implementation_data = _read_repo_binding(repository_root, implementation)
    _validate_preview_implementation(implementation_data, PREVIEW_API_SYMBOLS)

    dependencies_raw = preview.get("dependencies")
    require(
        isinstance(dependencies_raw, dict) and set(dependencies_raw) == {"human_diff_renderer"},
        "CODEC_GATE_INVALID",
        "G1.5 preview dependency binding is not closed",
    )
    dependencies = cast(dict[str, Any], dependencies_raw)
    human_diff_raw = dependencies.get("human_diff_renderer")
    require(
        isinstance(human_diff_raw, dict)
        and set(human_diff_raw) == {"path", "sha256"}
        and human_diff_raw.get("path")
        == "MobileWorld/src/mobile_world/offline/g1_history_codecs/diff.py",
        "CODEC_GATE_INVALID",
        "G1.5 human-diff dependency binding differs",
    )
    human_diff = cast(dict[str, Any], human_diff_raw)
    _read_repo_binding(repository_root, human_diff)

    output_schema_raw = preview.get("output_schema")
    require(
        isinstance(output_schema_raw, dict)
        and set(output_schema_raw) == {"path", "sha256"}
        and output_schema_raw.get("path")
        == "mobileworld_audit_handoff/schemas/g1_5/history_codec_preview.schema.json",
        "CODEC_GATE_INVALID",
        "G1.5 preview output-schema binding differs",
    )
    output_schema = cast(dict[str, Any], output_schema_raw)
    _validate_preview_output_schema(_read_repo_binding(repository_root, output_schema))

    require(
        preview.get("outputs")
        == {
            "strict_five_arm": True,
            "clean_original_sham": True,
            "exact_correction_anchors": True,
            "correction_token_ranking": True,
            "sham_token_match": True,
            "target_only_diff": True,
            "reversible_mapping": True,
            "full_request_browser_projection_allowed": False,
        },
        "CODEC_GATE_INVALID",
        "G1.5 preview output capability envelope differs",
    )
    require(
        preview.get("tokenizer_policy")
        == {
            "caller_injected_local_pinned_counter": True,
            "special_tokens_enabled": False,
            "unavailable_reason_code": "PINNED_TOKENIZER_UNAVAILABLE",
            "download_allowed": False,
            "substitution_allowed": False,
            "human_entered_count_allowed": False,
        },
        "CODEC_GATE_INVALID",
        "G1.5 preview tokenizer policy is not fail-closed",
    )

    tokenizer_bindings_raw = preview.get("pinned_tokenizers")
    require(
        isinstance(tokenizer_bindings_raw, list) and len(tokenizer_bindings_raw) == 2,
        "CODEC_GATE_INVALID",
        "G1.5 preview must bind exactly the Qwen and MAI tokenizers",
    )
    model_manifest_data = _read_repo_binding(repository_root, PREVIEW_MODEL_CONFIG_MANIFEST)
    model_manifest = _parse_codec_gate_json_object(
        model_manifest_data, "frozen G1.1 model/config manifest"
    )
    models_raw = model_manifest.get("models")
    require(
        model_manifest.get("artifact_type") == "g1_model_configuration_manifest"
        and model_manifest.get("schema_version") == "mobileworld.g1.causal-replay/model-config-v1"
        and isinstance(models_raw, list)
        and len(models_raw) == 2
        and all(isinstance(model, dict) for model in models_raw),
        "CODEC_GATE_INVALID",
        "frozen G1.1 model/config manifest identity or model set differs",
    )
    models = cast(list[dict[str, Any]], models_raw)
    model_ids = [model.get("model_id") for model in models]
    require(
        model_ids == ["qwen3vl_8b", "mai_ui_8b"] and len(model_ids) == len(set(model_ids)),
        "CODEC_GATE_INVALID",
        "frozen G1.1 model/config manifest model order or identity differs",
    )
    model_by_id = {cast(str, model["model_id"]): model for model in models}

    verified_tokenizers: list[dict[str, Any]] = []
    tokenizer_bindings = cast(list[Any], tokenizer_bindings_raw)
    for raw_binding, expected in zip(tokenizer_bindings, PREVIEW_TOKENIZER_SPECS, strict=True):
        require(
            isinstance(raw_binding, dict)
            and set(raw_binding)
            == {
                "model_id",
                "history_family",
                "tokenizer_id",
                "tokenizer_revision",
                "tokenizer_binding_sha256",
                "model_config_manifest",
                "counting_call",
                "special_tokens_enabled",
                "local_artifact_verification_required",
                "unavailable_reason_code",
            },
            "CODEC_GATE_INVALID",
            "G1.5 preview tokenizer binding is not closed",
        )
        binding = cast(dict[str, Any], raw_binding)
        require(
            all(binding.get(key) == expected[key] for key in expected)
            and binding.get("model_config_manifest") == PREVIEW_MODEL_CONFIG_MANIFEST
            and binding.get("counting_call") == "tokenizer.encode(text, add_special_tokens=False)"
            and binding.get("special_tokens_enabled") is False
            and binding.get("local_artifact_verification_required") is True
            and binding.get("unavailable_reason_code") == "PINNED_TOKENIZER_UNAVAILABLE",
            "CODEC_GATE_INVALID",
            "G1.5 preview tokenizer identity or safety binding differs",
        )
        model = model_by_id[cast(str, binding["model_id"])]
        tokenizer = model.get("tokenizer")
        require(
            isinstance(tokenizer, dict)
            and model.get("history_family") == binding["history_family"]
            and model.get("model_repository") == binding["tokenizer_id"]
            and model.get("model_revision") == binding["tokenizer_revision"]
            and tokenizer.get("revision") == binding["tokenizer_revision"]
            and tokenizer.get("counting_call") == binding["counting_call"],
            "CODEC_GATE_INVALID",
            "G1.5 preview tokenizer does not match its frozen model record",
        )
        try:
            tokenizer_record_sha256 = canonical_sha256(tokenizer)
        except (TypeError, ValueError) as exc:
            raise CurationError(
                "CODEC_GATE_INVALID", "frozen tokenizer record is not canonical JSON"
            ) from exc
        require(
            tokenizer_record_sha256 == binding["tokenizer_binding_sha256"],
            "CODEC_GATE_INVALID",
            "G1.5 preview tokenizer binding digest differs from the frozen model record",
        )
        verified_tokenizers.append(
            {
                "model_id": binding["model_id"],
                "history_family": binding["history_family"],
                "tokenizer_id": binding["tokenizer_id"],
                "tokenizer_revision": binding["tokenizer_revision"],
                "tokenizer_binding_sha256": tokenizer_record_sha256,
            }
        )

    return {
        "schema_version": preview["schema_version"],
        "preview_api_sha256": canonical_sha256(preview),
        "implementation_sha256": implementation["sha256"],
        "human_diff_renderer_sha256": human_diff["sha256"],
        "output_schema_sha256": output_schema["sha256"],
        "model_config_manifest_sha256": PREVIEW_MODEL_CONFIG_MANIFEST["sha256"],
        "tokenizer_policy_sha256": canonical_sha256(preview["tokenizer_policy"]),
        "pinned_tokenizers": verified_tokenizers,
    }


def _load_g1_5_publication(
    manifest_path: Path, repository_root: Path
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    require(
        not manifest_path.is_symlink(),
        "CODEC_GATE_INVALID",
        "G1.5 CPU publication manifest cannot be a symlink",
    )
    try:
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise CurationError("CODEC_GATE_INVALID", "G1.5 CPU publication is missing") from exc
    require(
        _is_within(resolved_manifest, repository_root),
        "CODEC_GATE_INVALID",
        "G1.5 CPU publication must be checked in under the repository root",
    )
    relative_manifest = resolved_manifest.relative_to(repository_root)
    current = repository_root
    for part in relative_manifest.parts:
        current = current / part
        require(
            not current.is_symlink(),
            "CODEC_GATE_INVALID",
            "G1.5 CPU publication path traverses a symlink",
        )
    data = _read_regular(resolved_manifest)
    assert data is not None
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("CODEC_GATE_INVALID", "G1.5 publication is invalid JSON") from exc
    require(
        isinstance(value, dict)
        and value.get("schema_version") == "mobileworld.g1.history-codec-cpu-publication/v1"
        and value.get("issue") == "ALE-323"
        and value.get("story") == "G1.5"
        and value.get("publication_scope") == "SECRET_FREE_CPU_CONFORMANCE_ONLY"
        and value.get("status") == "CPU_CHECKPOINT_IMPLEMENTED_LIVE_SMOKE_DEFERRED",
        "CODEC_GATE_INVALID",
        "G1.5 publication identity or scope differs",
    )
    publication = cast(dict[str, Any], value)
    preview_api_binding = _validate_preview_api(publication, repository_root)
    selected_raw = publication.get("selected_codecs")
    require(
        isinstance(selected_raw, list) and len(selected_raw) == 2,
        "CODEC_GATE_INVALID",
        "G1.5 publication must select exactly one codec per history family",
    )
    selected = cast(list[Any], selected_raw)
    require(
        {item.get("history_family") for item in selected if isinstance(item, dict)}
        == {"flat_progress", "raw_replay"},
        "CODEC_GATE_INVALID",
        "G1.5 publication must select exactly one codec per history family",
    )
    shared_raw = publication.get("shared_bindings")
    require(
        isinstance(shared_raw, dict)
        and set(shared_raw) == {"history_ir_schema", "renderer", "tokenizer_binding"},
        "CODEC_GATE_INVALID",
        "G1.5 shared binding shape differs",
    )
    shared = cast(dict[str, Any], shared_raw)
    for reference in shared.values():
        require(isinstance(reference, dict), "CODEC_GATE_INVALID", "shared binding is invalid")
        _read_repo_binding(repository_root, reference)
    tokenizer = shared["tokenizer_binding"]
    require(
        tokenizer.get("tokenizer_required") is False,
        "CODEC_GATE_INVALID",
        "G1.5 CPU gate unexpectedly requires a tokenizer/model load",
    )
    codec_ids: set[str] = set()
    for codec_raw in selected:
        require(
            isinstance(codec_raw, dict)
            and set(codec_raw)
            == {
                "codec_id",
                "codec_contract_version",
                "history_family",
                "implementation",
                "capability",
                "source_fixture",
                "conformance_receipt",
            },
            "CODEC_GATE_INVALID",
            "selected codec binding shape differs",
        )
        codec = cast(dict[str, Any], codec_raw)
        codec_id = codec["codec_id"]
        require(
            isinstance(codec_id, str)
            and codec_id not in codec_ids
            and codec["codec_contract_version"] == "v1",
            "CODEC_GATE_INVALID",
            "selected codec identity differs",
        )
        codec_ids.add(codec_id)
        _read_repo_binding(repository_root, codec["implementation"])
        _read_repo_binding(repository_root, codec["source_fixture"])
        declaration = codec["capability"].get("declaration")
        require(
            isinstance(declaration, dict)
            and canonical_sha256(declaration) == codec["capability"].get("sha256")
            and declaration.get("codec_id") == codec_id
            and declaration.get("contract_version") == "v1"
            and declaration.get("history_family") == codec["history_family"]
            and declaration.get("level") == "VALIDITY_TRANSFORMATION"
            and declaration.get("supported_operations") == ["DROP", "REPLACE"]
            and set(declaration.get("supported_arms", []))
            == {"ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"}
            and declaration.get("live_ready") is False,
            "CODEC_GATE_INVALID",
            "selected codec capability declaration is invalid",
        )
        receipt_data = _read_repo_binding(repository_root, codec["conformance_receipt"])
        try:
            receipt = json.loads(receipt_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CurationError(
                "CODEC_GATE_INVALID", "G1.5 conformance receipt is invalid JSON"
            ) from exc
        require(
            isinstance(receipt, dict)
            and receipt.get("codec_id") == codec_id
            and receipt.get("codec_contract_version") == "v1"
            and receipt.get("history_family") == codec["history_family"]
            and receipt.get("capability_sha256") == codec["capability"]["sha256"]
            and receipt.get("checkpoint_scope") == "CPU_ONLY"
            and receipt.get("provider_invocation_allowed") is False
            and receipt.get("provider_invocation_count") == 0
            and receipt.get("treatment_response_count") == 0
            and receipt.get("network_used") is False
            and receipt.get("gpu_used") is False
            and receipt.get("gui_action_executed") is False
            and isinstance(receipt.get("arms"), list)
            and len(receipt["arms"]) == 5
            and all(isinstance(arm, dict) for arm in receipt["arms"])
            and {arm.get("arm") for arm in receipt["arms"]}
            == {"ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"}
            and all(
                arm.get("provider_invocation_allowed") is False
                and arm.get("target_only_diff") is True
                and arm.get("source_mapping_reversible") is True
                for arm in receipt["arms"]
            ),
            "CODEC_GATE_INVALID",
            "G1.5 conformance receipt does not satisfy the CPU gate",
        )
    safety = publication.get("safety")
    require(
        isinstance(safety, dict)
        and safety.get("formal_g1_data") is False
        and safety.get("live_smoke_completed") is False
        and safety.get("provider_invocation_allowed") is False
        and safety.get("provider_invocation_count") == 0
        and safety.get("treatment_response_generation_allowed") is False
        and safety.get("treatment_response_count") == 0
        and safety.get("network_used") is False
        and safety.get("gpu_used") is False
        and safety.get("gui_action_executed") is False,
        "CODEC_GATE_INVALID",
        "G1.5 publication safety guard differs",
    )
    return publication, data, preview_api_binding


def build_codec_gate_receipt(
    manifest_path: str | os.PathLike[str],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    repository = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
    publication, manifest_bytes, preview_api_binding = _load_g1_5_publication(
        Path(manifest_path), repository
    )
    selected = {item["history_family"]: item for item in publication["selected_codecs"]}
    shared = publication["shared_bindings"]
    preview_tokenizers = {
        item["history_family"]: item for item in preview_api_binding["pinned_tokenizers"]
    }

    def binding(family: str) -> dict[str, Any]:
        codec = selected[family]
        tokenizer = preview_tokenizers[family]
        return {
            "history_family": family,
            "codec_id": codec["codec_id"],
            "codec_contract_version": codec["codec_contract_version"],
            "implementation_sha256": codec["implementation"]["sha256"],
            "capability_sha256": codec["capability"]["sha256"],
            "history_ir_schema_sha256": shared["history_ir_schema"]["sha256"],
            "renderer_sha256": shared["renderer"]["sha256"],
            "host_coordinate_binding_sha256": shared["tokenizer_binding"]["sha256"],
            "tokenizer_binding_sha256": tokenizer["tokenizer_binding_sha256"],
            "model_config_manifest_sha256": preview_api_binding["model_config_manifest_sha256"],
            "conformance_receipt_sha256": codec["conformance_receipt"]["file_sha256"],
        }

    subject = {
        "schema_version": CODEC_GATE_SCHEMA_VERSION,
        "record_type": "gold_curation_codec_gate",
        "g1_5_publication_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "qwen_codec_binding": binding("flat_progress"),
        "mai_codec_binding": binding("raw_replay"),
        "preview_api_binding": preview_api_binding,
        "checks": json_copy(CODEC_GATE_CHECKS),
    }
    return {**subject, "receipt_sha256": canonical_sha256(subject)}


def write_codec_gate_receipt(
    manifest_path: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> Path:
    repository = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
    root = _ensure_root(Path(output_root), (repository, ACTIVE_G1_3_PUBLICATION))
    receipt = build_codec_gate_receipt(manifest_path, repository_root=repository)
    digest = receipt["receipt_sha256"]
    prefix = _ensure_child_directory(_ensure_child_directory(root, "sha256"), digest[:2])
    path = prefix / f"{digest}.json"
    _write_once_regular(path, canonical_json_bytes(receipt))
    return path


def _load_codec_gate_receipt(
    receipt_path: Path,
    manifest_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    require(
        not receipt_path.is_symlink(),
        "CODEC_GATE_INVALID",
        "codec gate receipt cannot be a symlink",
    )
    try:
        resolved = receipt_path.resolve(strict=True)
    except OSError as exc:
        raise CurationError("CODEC_GATE_INVALID", "codec gate receipt is missing") from exc
    for forbidden in (repository_root, ACTIVE_G1_3_PUBLICATION.resolve(strict=True)):
        require(
            not _is_within(resolved, forbidden),
            "CODEC_GATE_INVALID",
            "codec gate receipt must be repository-external",
        )
    data = _read_regular(resolved, owner_restricted=True)
    assert data is not None
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurationError("CODEC_GATE_INVALID", "codec gate receipt is invalid JSON") from exc
    require(
        isinstance(value, dict) and data == canonical_json_bytes(value),
        "CODEC_GATE_INVALID",
        "codec gate receipt must be canonical JSON without a trailing newline",
    )
    receipt = cast(dict[str, Any], value)
    expected = build_codec_gate_receipt(manifest_path, repository_root=repository_root)
    require(
        receipt == expected,
        "CODEC_GATE_INVALID",
        "codec gate receipt differs from the verified G1.5 CPU publication",
    )
    digest = receipt["receipt_sha256"]
    require(
        resolved.name == f"{digest}.json"
        and resolved.parent.name == digest[:2]
        and resolved.parent.parent.name == "sha256",
        "CODEC_GATE_INVALID",
        "codec gate receipt path is not content-addressed",
    )
    return receipt


@dataclass(frozen=True, slots=True)
class ReviewerRegistry:
    """Owner-controlled, repo-external mapping from one principal to one role."""

    canonical_bytes: bytes
    sha256: str
    _principals: tuple[tuple[str, str, str, str | None], ...]
    source_path: Path

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ReviewerRegistry:
        supplied = Path(path)
        require(
            not supplied.is_symlink(),
            "REVIEWER_REGISTRY_INVALID",
            "reviewer registry cannot be a symlink",
        )
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as exc:
            raise CurationError(
                "REVIEWER_REGISTRY_INVALID", "reviewer registry does not exist"
            ) from exc
        repository_root = Path(__file__).resolve().parents[5]
        for forbidden in (repository_root, ACTIVE_G1_3_PUBLICATION):
            forbidden_resolved = forbidden.resolve(strict=False)
            require(
                not _is_within(resolved, forbidden_resolved),
                "REVIEWER_REGISTRY_INVALID",
                "reviewer registry must be repository-external",
            )
        registry_stat = supplied.stat(follow_symlinks=False)
        require(
            stat.S_ISREG(registry_stat.st_mode)
            and registry_stat.st_uid == os.geteuid()
            and registry_stat.st_nlink == 1
            and registry_stat.st_mode & 0o077 == 0,
            "REVIEWER_REGISTRY_INVALID",
            "reviewer registry must be owner-only, regular, and singly linked",
        )
        data = _read_regular(supplied, owner_restricted=True)
        assert data is not None
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CurationError(
                "REVIEWER_REGISTRY_INVALID", "reviewer registry is not valid JSON"
            ) from exc
        require(
            isinstance(value, dict)
            and set(value) == {"schema_version", "principals"}
            and value.get("schema_version") == REVIEWER_REGISTRY_SCHEMA_VERSION,
            "REVIEWER_REGISTRY_INVALID",
            "reviewer registry envelope is invalid",
        )
        raw_principals = value.get("principals")
        require(
            isinstance(raw_principals, list) and bool(raw_principals),
            "REVIEWER_REGISTRY_INVALID",
            "reviewer registry must contain principals",
        )
        principals: list[tuple[str, str, str, str | None]] = []
        for raw in raw_principals:
            require(
                isinstance(raw, dict)
                and set(raw) == {"principal_id", "role", "access_secret", "adjudication_channel"},
                "REVIEWER_REGISTRY_INVALID",
                "reviewer principal record is not closed",
            )
            principal_id, role = validate_identity(raw["principal_id"], raw["role"])
            secret = raw["access_secret"]
            adjudication_channel = raw["adjudication_channel"]
            require(
                isinstance(secret, str) and len(secret.encode("utf-8")) >= 16,
                "REVIEWER_REGISTRY_INVALID",
                "reviewer access secret must contain at least 16 UTF-8 bytes",
            )
            if role == ADJUDICATOR_ROLE:
                require(
                    adjudication_channel in CHANNELS,
                    "REVIEWER_REGISTRY_INVALID",
                    "adjudicator must be owner-bound to exactly one channel",
                )
            else:
                require(
                    adjudication_channel is None,
                    "REVIEWER_REGISTRY_INVALID",
                    "blind reviewer cannot have an adjudication channel",
                )
            principals.append((principal_id, role, secret, adjudication_channel))
        principal_ids = [principal_id for principal_id, _, _, _ in principals]
        require(
            len(principal_ids) == len(set(principal_ids)),
            "REVIEWER_REGISTRY_INVALID",
            "one canonical principal cannot have aliases or multiple roles",
        )
        semantic_value = {
            "schema_version": REVIEWER_REGISTRY_SCHEMA_VERSION,
            "principals": [
                {
                    "principal_id": principal_id,
                    "role": role,
                    "access_secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                    "adjudication_channel": adjudication_channel,
                }
                for principal_id, role, secret, adjudication_channel in sorted(principals)
            ],
        }
        canonical = canonical_json_bytes(semantic_value)
        return cls(
            canonical_bytes=canonical,
            sha256=hashlib.sha256(canonical).hexdigest(),
            _principals=tuple(principals),
            source_path=resolved,
        )

    def role_for(self, principal_id: str) -> str:
        matches = [role for candidate, role, _, _ in self._principals if candidate == principal_id]
        require(
            len(matches) == 1,
            "REVIEWER_AUTHENTICATION_FAILED",
            "reviewer principal is not in the owner registry",
        )
        return matches[0]

    def authenticate(self, principal_id: Any, role: Any, access_secret: Any) -> tuple[str, str]:
        principal_id, role = validate_identity(principal_id, role)
        require(
            isinstance(access_secret, str),
            "REVIEWER_AUTHENTICATION_FAILED",
            "reviewer access secret is invalid",
        )
        matches = [
            (candidate_role, secret)
            for candidate, candidate_role, secret, _ in self._principals
            if candidate == principal_id
        ]
        require(
            len(matches) == 1
            and hmac.compare_digest(matches[0][0], role)
            and hmac.compare_digest(matches[0][1], access_secret),
            "REVIEWER_AUTHENTICATION_FAILED",
            "reviewer principal, role, or access secret is invalid",
        )
        return principal_id, role

    def adjudication_channel_for(self, principal_id: str) -> str:
        matches = [
            channel
            for candidate, role, _, channel in self._principals
            if candidate == principal_id and role == ADJUDICATOR_ROLE
        ]
        require(
            len(matches) == 1 and matches[0] in CHANNELS,
            "REVIEWER_AUTHENTICATION_FAILED",
            "adjudicator is not owner-bound to a channel",
        )
        return cast(str, matches[0])


class AnnotationStore:
    """Authoritative local journal for blind reviews and adjudications."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        publication: CurationPublication,
        reviewer_registry: ReviewerRegistry,
        *,
        repository_root: str | os.PathLike[str] | None = None,
        codec_gate_receipt_path: str | os.PathLike[str] | None = None,
        g1_5_publication_manifest_path: str | os.PathLike[str] | None = None,
    ) -> None:
        repo = Path(repository_root or Path(__file__).resolve().parents[5]).resolve(strict=True)
        self.root = _ensure_root(Path(root), (repo, ACTIVE_G1_3_PUBLICATION))
        self.publication = publication
        self._journal = self.root / "annotation-events.jsonl"
        self._manifest = self.root / "workspace-manifest.json"
        self._assignment_key_path = self.root / "assignment-key.bin"
        self.reviewer_registry = reviewer_registry
        self._repository_root = repo
        require(
            (codec_gate_receipt_path is None) == (g1_5_publication_manifest_path is None),
            "CODEC_GATE_INVALID",
            "codec gate receipt and G1.5 publication manifest must be supplied together",
        )
        self._codec_gate_receipt_path = (
            None if codec_gate_receipt_path is None else Path(codec_gate_receipt_path).absolute()
        )
        self._g1_5_publication_manifest_path = (
            None
            if g1_5_publication_manifest_path is None
            else Path(g1_5_publication_manifest_path).absolute()
        )
        self.codec_gate_receipt = (
            None
            if self._codec_gate_receipt_path is None
            else _load_codec_gate_receipt(
                self._codec_gate_receipt_path,
                cast(Path, self._g1_5_publication_manifest_path),
                repo,
            )
        )
        self._assignment_key = self._load_or_create_assignment_key()
        self._identity_key_commitment_sha256 = hashlib.sha256(self._assignment_key).hexdigest()
        self.workspace_id = (
            "g1workspace-"
            + canonical_sha256(
                {
                    "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
                    "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
                    "owner_registry_sha256": reviewer_registry.sha256,
                    "identity_key_commitment_sha256": self._identity_key_commitment_sha256,
                }
            )[:24]
        )
        self._bind_manifest()

    @property
    def formal_annotation_open(self) -> bool:
        return self._verified_codec_gate_receipt_sha256() is not None

    def _verified_codec_gate_receipt_sha256(self) -> str | None:
        """Revalidate the configured G1.5 gate and return its exact receipt digest."""

        if self.codec_gate_receipt is None:
            require(
                self._codec_gate_receipt_path is None
                and self._g1_5_publication_manifest_path is None,
                "CODEC_GATE_INVALID",
                "codec gate runtime binding is inconsistent",
            )
            return None
        require(
            self._codec_gate_receipt_path is not None
            and self._g1_5_publication_manifest_path is not None,
            "CODEC_GATE_INVALID",
            "codec gate runtime binding is incomplete",
        )
        assert self._codec_gate_receipt_path is not None
        assert self._g1_5_publication_manifest_path is not None
        current = _load_codec_gate_receipt(
            self._codec_gate_receipt_path,
            self._g1_5_publication_manifest_path,
            self._repository_root,
        )
        require(
            current == self.codec_gate_receipt,
            "CODEC_GATE_INVALID",
            "codec gate receipt changed after workspace bootstrap",
        )
        return cast(str, current["receipt_sha256"])

    def _require_codec_gate(self) -> str:
        digest = self._verified_codec_gate_receipt_sha256()
        require(
            digest is not None,
            "CODEC_GATE_NOT_OPEN",
            "final review is blocked until the G1.5 Qwen and MAI CPU codec gate is verified",
        )
        return cast(str, digest)

    def _load_or_create_assignment_key(self) -> bytes:
        existing = _read_regular(self._assignment_key_path, missing_ok=True, owner_restricted=True)
        if existing is not None:
            require(
                len(existing) == 32,
                "WORKSPACE_BINDING_MISMATCH",
                "assignment key has the wrong length",
            )
            return existing
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._assignment_key_path, flags, 0o600)
        except FileExistsError:
            existing = _read_regular(self._assignment_key_path, owner_restricted=True)
            require(
                existing is not None and len(existing) == 32,
                "WORKSPACE_BINDING_MISMATCH",
                "assignment key collision",
            )
            return cast(bytes, existing)
        except OSError as exc:
            raise CurationError(
                "ANNOTATION_STORE_INVALID", "assignment key cannot be created safely"
            ) from exc
        try:
            _write_all(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    def _bind_manifest(self) -> None:
        value = {
            "schema_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
            "record_type": "gold_curation_workspace_manifest",
            "workspace_id": self.workspace_id,
            "workspace_version": 1,
            "previous_workspace_manifest_sha256": None,
            "supersession_reason": None,
            "contract_version": "mobileworld.g1.gold-history-intervention/contract-v1",
            "issue": "ALE-324",
            "story": "G1.6",
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
            "strict_mhr_count": 152,
            "selected_clean_control_count": 38,
            "target_unit_count": 190,
            "reserve_clean_control_out_of_scope_count": 38,
            "qwen_target_count": 169,
            "mai_target_count": 21,
            "formal_schema_sha256s": _formal_schema_hashes(),
            "identity_policy": {
                "identity_key_commitment_sha256": self._identity_key_commitment_sha256,
                "owner_registry_sha256": self.reviewer_registry.sha256,
                "identity_commitment_formula_id": "mobileworld.g1.workspace-principal-hmac-sha256/v1",
                "assignment_id_formula_id": "mobileworld.g1.workspace-assignment-hmac-sha256/v1",
                "one_workspace_key": True,
                "canonical_principal_contract": "OWNER_REGISTRY_STABLE_UTF8_IDENTIFIER_EXACT_BYTES_V1",
                "owner_registry_authoritative": True,
                "adjudicator_channel_registry_bound": True,
                "client_supplied_identity_accepted": False,
                "aliases_for_same_principal_allowed": False,
                "http_only_session_required": True,
            },
            "storage_policy": {
                "repo_external": True,
                "append_only_canonical_jsonl": True,
                "atomic_no_replace": True,
                "no_follow": True,
                "regular_files_only": True,
                "path_escape_rejected": True,
                "authoritative_browser_storage": False,
                "authoritative_records_mutable": False,
            },
            "server_policy": {
                "allowed_bind_hosts": ["127.0.0.1"],
                "wildcard_bind_allowed": False,
                "non_loopback_peer_allowed": False,
                "external_hosting_allowed": False,
                "external_network_allowed": False,
                "remote_assets_allowed": False,
                "same_origin_required": True,
                "csrf_protection_required": True,
                "http_only_session_cookie_required": True,
                "single_process_foreground_server_allowed": True,
                "child_worker_or_reloader_allowed": False,
                "packet_scoped_evidence_authorization": True,
            },
            "review_policy": {
                "reviewed_channels": ["ACTION_GOLD", "TRANSFORMATION", "CONSISTENCY_AUDIT"],
                "formal_curation_channels": ["ACTION_GOLD", "TRANSFORMATION"],
                "descriptive_channels": ["CONSISTENCY_AUDIT"],
                "initial_review_stages": ["PRIMARY", "SECONDARY"],
                "independent_review_count_per_channel": 2,
                "peer_proposal_visible_before_both_finalize": False,
                "material_disagreement_requires_adjudication": True,
                "adjudicator_identity_disjoint": True,
                "cross_channel_identity_disjoint": True,
                "semantic_choices_human_only": True,
                "consistency_audit_opens_after_formal_channels_resolve": True,
                "consistency_audit_may_affect_admission": False,
            },
            "readiness": {
                "workspace_initialized": True,
                "formal_annotation_open": False,
                "codec_publication_required_for_formal_annotation": True,
                "curation_and_admission_sealed": False,
                "admission_ready": False,
                "execution_ready": False,
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
                "formal_replay_ready": False,
            },
            "safety": {
                "external_network_used": False,
                "provider_client_created": False,
                "provider_invoked": False,
                "gpu_probed": False,
                "gpu_used": False,
                "model_loaded": False,
                "replay_executed": False,
                "mobileworld_gui_or_tool_action_executed": False,
                "treatment_response_count": 0,
                "automatic_semantic_inference_performed": False,
                "raw_or_frozen_artifact_mutated": False,
            },
        }
        validate_schema_record("annotation_workspace.schema.json", value)
        data = canonical_json_bytes(value) + b"\n"
        self._workspace_manifest_bytes = data
        existing = _read_regular(self._manifest, missing_ok=True, owner_restricted=True)
        if existing is not None:
            require(
                existing == data,
                "WORKSPACE_BINDING_MISMATCH",
                "annotation workspace binds different source bytes",
            )
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._manifest, flags, 0o600)
        except FileExistsError:
            existing = _read_regular(self._manifest, owner_restricted=True)
            require(
                existing == data,
                "WORKSPACE_BINDING_MISMATCH",
                "annotation workspace manifest collision",
            )
            return
        except OSError as exc:
            raise CurationError(
                "ANNOTATION_STORE_INVALID", "workspace manifest cannot be created safely"
            ) from exc
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _assert_workspace_manifest(self) -> None:
        current = _read_regular(self._manifest, owner_restricted=True)
        require(
            current == self._workspace_manifest_bytes,
            "WORKSPACE_BINDING_MISMATCH",
            "annotation workspace manifest changed after bootstrap",
        )

    @staticmethod
    def _decode_events(data: bytes) -> list[dict[str, Any]]:
        if not data:
            return []
        require(
            data.endswith(b"\n") and b"\r" not in data,
            "ANNOTATION_LEDGER_INVALID",
            "annotation ledger must use exactly one LF after every canonical event",
        )
        events: list[dict[str, Any]] = []
        previous: str | None = None
        for index, raw in enumerate(data[:-1].split(b"\n")):
            require(
                bool(raw), "ANNOTATION_LEDGER_INVALID", "annotation ledger contains a blank record"
            )
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CurationError(
                    "ANNOTATION_LEDGER_INVALID", "annotation ledger contains invalid JSON"
                ) from exc
            require(
                isinstance(value, dict),
                "ANNOTATION_LEDGER_INVALID",
                "annotation event must be an object",
            )
            event = cast(dict[str, Any], value)
            require(
                raw == canonical_json_bytes(event),
                "ANNOTATION_LEDGER_INVALID",
                "annotation event is not canonical JSON",
            )
            required = {
                "schema_version",
                "record_type",
                "event_id",
                "event_seq",
                "previous_event_sha256",
                "event_sha256",
                "event_kind",
                "created_at_ns",
                "unit_id",
                "channel",
                "assignment_id",
                "source_packet_sha256",
                "assignment_packet_sha256",
                "reviewer_identity_sha256",
                "reviewer_role",
                "proposal_schema_version",
                "codec_gate_receipt_sha256",
                "payload",
                "payload_sha256",
                "material_projection_sha256",
            }
            require(
                set(event) == required,
                "ANNOTATION_LEDGER_INVALID",
                "annotation event shape is not closed",
            )
            require(
                event["schema_version"] == ANNOTATION_EVENT_SCHEMA_VERSION
                and event["record_type"] == "gold_curation_annotation_event",
                "ANNOTATION_LEDGER_INVALID",
                "annotation event version is invalid",
            )
            require(
                event["event_seq"] == index and event["previous_event_sha256"] == previous,
                "ANNOTATION_LEDGER_INVALID",
                "annotation event chain is discontinuous",
            )
            subject = {key: val for key, val in event.items() if key != "event_sha256"}
            require(
                event["event_sha256"] == canonical_sha256(subject),
                "ANNOTATION_LEDGER_INVALID",
                "annotation event digest differs",
            )
            id_subject = {
                key: val for key, val in event.items() if key not in {"event_id", "event_sha256"}
            }
            require(
                event["event_id"] == "g1annotation-" + canonical_sha256(id_subject)[:24],
                "ANNOTATION_LEDGER_INVALID",
                "annotation event ID differs",
            )
            require(
                event["event_kind"] in EVENT_KINDS,
                "ANNOTATION_LEDGER_INVALID",
                "annotation event kind is invalid",
            )
            require(
                event["proposal_schema_version"] == REVIEW_PROPOSAL_SCHEMA_VERSION,
                "ANNOTATION_LEDGER_INVALID",
                "proposal version is invalid",
            )
            require(
                event["payload_sha256"] == canonical_sha256(event["payload"]),
                "ANNOTATION_LEDGER_INVALID",
                "annotation payload digest differs",
            )
            require(
                isinstance(event["reviewer_identity_sha256"], str)
                and len(event["reviewer_identity_sha256"]) == 64
                and all(char in "0123456789abcdef" for char in event["reviewer_identity_sha256"]),
                "ANNOTATION_LEDGER_INVALID",
                "reviewer identity commitment is invalid",
            )
            require(
                event["reviewer_role"] in (*REVIEW_ROLES, ADJUDICATOR_ROLE),
                "ANNOTATION_LEDGER_INVALID",
                "reviewer role is invalid",
            )
            validate_schema_record("annotation_event.schema.json", event)
            proposal = (
                event["payload"]["resolved_payload"]
                if event["event_kind"] == "ADJUDICATION_SUBMITTED"
                else event["payload"]
            )
            validate_schema_record("review_proposal.schema.json", proposal)
            previous = event["event_sha256"]
            events.append(event)
        return events

    def read_events(self) -> list[dict[str, Any]]:
        self._assert_workspace_manifest()
        data = _read_regular(self._journal, missing_ok=True, owner_restricted=True)
        events = [] if data is None else self._decode_events(data)
        self._validate_event_semantics(events)
        return events

    def _assert_source_packet_ref(self, digest: str) -> bytes:
        require(
            len(digest) == 64 and all(char in "0123456789abcdef" for char in digest),
            "ANNOTATION_LEDGER_INVALID",
            "source packet digest is invalid",
        )
        path = self.root / "packets" / "sha256" / digest[:2] / f"{digest}.json"
        data = _read_regular(path, owner_restricted=True)
        assert data is not None
        require(
            hashlib.sha256(data).hexdigest() == digest,
            "ANNOTATION_LEDGER_INVALID",
            "source packet artifact digest differs",
        )
        return data

    def _assert_assignment_packet_ref(self, digest: str) -> dict[str, Any]:
        require(
            len(digest) == 64 and all(char in "0123456789abcdef" for char in digest),
            "ANNOTATION_LEDGER_INVALID",
            "assignment packet digest is invalid",
        )
        path = self.root / "assignment-packets" / "sha256" / digest[:2] / f"{digest}.json"
        data = _read_regular(path, owner_restricted=True)
        assert data is not None
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CurationError(
                "ANNOTATION_LEDGER_INVALID", "assignment packet artifact is invalid JSON"
            ) from exc
        require(
            isinstance(value, dict) and data == canonical_json_bytes(value),
            "ANNOTATION_LEDGER_INVALID",
            "assignment packet artifact is not canonical JSON",
        )
        packet = cast(dict[str, Any], value)
        validate_schema_record("curator_packet.schema.json", packet)
        subject = {key: item for key, item in packet.items() if key != "assignment_packet_sha256"}
        require(
            canonical_sha256(subject) == digest and packet["assignment_packet_sha256"] == digest,
            "ANNOTATION_LEDGER_INVALID",
            "assignment packet artifact digest differs",
        )
        return packet

    def _resolution_set_sha256_from_events(self, events: list[dict[str, Any]], unit_id: str) -> str:
        resolved: dict[str, Any] = {}
        for channel in ("ACTION_GOLD", "TRANSFORMATION"):
            resolution = self._channel_resolution(events, unit_id, channel)
            require(
                resolution is not None,
                "ANNOTATION_LEDGER_INVALID",
                "consistency packet precedes both formal channel resolutions",
            )
            assert resolution is not None
            resolved[channel] = {
                "resolution_kind": resolution["resolution_kind"],
                "payload_sha256": canonical_sha256(resolution["payload"]),
                "review_event_ids": resolution["review_event_ids"],
                "adjudication_event_id": resolution.get("adjudication_event_id"),
            }
        return canonical_sha256(
            {
                "unit_id": unit_id,
                "formal_curation_resolution_set": resolved,
                "descriptive_consistency_may_affect_admission": False,
            }
        )

    def _registered_principal_for_event(self, event: Mapping[str, Any]) -> str:
        matches = [
            principal_id
            for principal_id, role, _, adjudication_channel in self.reviewer_registry._principals
            if role == event["reviewer_role"]
            and (role != ADJUDICATOR_ROLE or adjudication_channel == event["channel"])
            and hmac.compare_digest(
                self.identity_commitment(principal_id), event["reviewer_identity_sha256"]
            )
        ]
        require(
            len(matches) == 1,
            "ANNOTATION_LEDGER_INVALID",
            "annotation event reviewer identity is not owner-registered for its role/channel",
        )
        return matches[0]

    def _validate_event_semantics(
        self, events: list[dict[str, Any]], *, start_index: int = 0
    ) -> None:
        """Re-prove every authoritative semantic and packet binding on each read."""

        units = {item["unit_id"]: item for item in self.publication.list_units()}
        require(
            type(start_index) is int and 0 <= start_index <= len(events),
            "ANNOTATION_LEDGER_INVALID",
            "annotation semantic validation start is invalid",
        )
        verified_codec_gate_sha256 = self._verified_codec_gate_receipt_sha256()
        for index in range(start_index, len(events)):
            event = events[index]
            prefix = events[:index]
            unit = units.get(event["unit_id"])
            require(
                unit is not None,
                "ANNOTATION_LEDGER_INVALID",
                "annotation event unit is outside the active publication",
            )
            assert unit is not None
            event_kind = event["event_kind"]
            channel = event["channel"]
            role = event["reviewer_role"]
            if event_kind == "DRAFT_SAVED":
                require(
                    event["codec_gate_receipt_sha256"] is None,
                    "ANNOTATION_LEDGER_INVALID",
                    "draft event must not claim formal codec gate provenance",
                )
            else:
                require(
                    verified_codec_gate_sha256 is not None
                    and event["codec_gate_receipt_sha256"] == verified_codec_gate_sha256,
                    "ANNOTATION_LEDGER_INVALID",
                    "formal annotation event does not bind the currently verified G1.5 CPU codec gate",
                )
            if event_kind == "ADJUDICATION_SUBMITTED":
                require(
                    role == ADJUDICATOR_ROLE,
                    "ANNOTATION_LEDGER_INVALID",
                    "adjudication event has a non-adjudicator role",
                )
                proposal = event["payload"]["resolved_payload"]
            else:
                require(
                    role in REVIEW_ROLES and role_channel(role) == channel,
                    "ANNOTATION_LEDGER_INVALID",
                    "review event role and channel differ",
                )
                proposal = event["payload"]
            principal_id = self._registered_principal_for_event(event)
            self._identity_guard(prefix, event["reviewer_identity_sha256"], role)
            validated = validate_review_payload(
                channel,
                proposal,
                clean_control=unit["unit_kind"] == "CLEAN_CONTROL",
            )
            require(
                validated == proposal,
                "ANNOTATION_LEDGER_INVALID",
                "annotation proposal differs from its canonical validated form",
            )
            self.publication.validate_review_payload_binding(event["unit_id"], channel, validated)
            if channel == "CONSISTENCY_AUDIT":
                require(
                    self._channels_resolved(
                        prefix, event["unit_id"], ("ACTION_GOLD", "TRANSFORMATION")
                    ),
                    "ANNOTATION_LEDGER_INVALID",
                    "consistency event precedes formal-channel resolution",
                )
            earlier_final = [
                prior
                for prior in prefix
                if prior["event_kind"] == "REVIEW_SUBMITTED"
                and prior["unit_id"] == event["unit_id"]
                and prior["reviewer_role"] == role
            ]
            if event_kind in {"DRAFT_SAVED", "REVIEW_SUBMITTED"}:
                require(
                    not earlier_final,
                    "ANNOTATION_LEDGER_INVALID",
                    "annotation journal contains a draft or duplicate final after finalization",
                )
            if event_kind == "REVIEW_SUBMITTED":
                counterpart = role.rsplit("_", 1)[0] + (
                    "_SECONDARY" if role.endswith("_PRIMARY") else "_PRIMARY"
                )
                require(
                    all(
                        prior["reviewer_identity_sha256"] != event["reviewer_identity_sha256"]
                        for prior in prefix
                        if prior["event_kind"] == "REVIEW_SUBMITTED"
                        and prior["unit_id"] == event["unit_id"]
                        and prior["reviewer_role"] == counterpart
                    ),
                    "ANNOTATION_LEDGER_INVALID",
                    "primary and secondary review identities are not independent",
                )
            expected_material = (
                None
                if event_kind == "DRAFT_SAVED"
                else canonical_sha256(
                    self.material_projection_for(event["unit_id"], channel, validated)
                )
            )
            require(
                event["material_projection_sha256"] == expected_material,
                "ANNOTATION_LEDGER_INVALID",
                "annotation material projection digest differs",
            )
            compared_review_event_ids: list[str] = []
            if event_kind == "ADJUDICATION_SUBMITTED":
                payload = event["payload"]
                reviews = self._final_reviews(prefix, event["unit_id"], channel)
                require(
                    set(reviews) == {"PRIMARY", "SECONDARY"}
                    and not any(
                        prior["event_kind"] == "ADJUDICATION_SUBMITTED"
                        and prior["unit_id"] == event["unit_id"]
                        and prior["channel"] == channel
                        for prior in prefix
                    ),
                    "ANNOTATION_LEDGER_INVALID",
                    "adjudication event has no unique pair of prior final reviews",
                )
                differences = self.disagreement_fields_for(
                    event["unit_id"],
                    channel,
                    reviews["PRIMARY"]["payload"],
                    reviews["SECONDARY"]["payload"],
                )
                require(
                    bool(differences)
                    and payload["disagreement_fields"] == differences
                    and payload["primary_event_id"] == reviews["PRIMARY"]["event_id"]
                    and payload["secondary_event_id"] == reviews["SECONDARY"]["event_id"]
                    and payload["primary_material_projection_sha256"]
                    == reviews["PRIMARY"]["material_projection_sha256"]
                    and payload["secondary_material_projection_sha256"]
                    == reviews["SECONDARY"]["material_projection_sha256"]
                    and event["reviewer_identity_sha256"]
                    not in {
                        reviews["PRIMARY"]["reviewer_identity_sha256"],
                        reviews["SECONDARY"]["reviewer_identity_sha256"],
                    },
                    "ANNOTATION_LEDGER_INVALID",
                    "adjudication references, projections, or disagreement fields differ",
                )
                compared_review_event_ids = [
                    reviews["PRIMARY"]["event_id"],
                    reviews["SECONDARY"]["event_id"],
                ]
            expected_assignment = self.assignment_id(
                event["unit_id"],
                role,
                channel=channel if role == ADJUDICATOR_ROLE else None,
            )
            require(
                event["assignment_id"] == expected_assignment,
                "ANNOTATION_LEDGER_INVALID",
                "annotation assignment binding differs",
            )
            resolution_sha = (
                self._resolution_set_sha256_from_events(prefix, event["unit_id"])
                if channel == "CONSISTENCY_AUDIT"
                else None
            )
            source_binding = self.publication.source_packet_binding(
                event["unit_id"],
                channel,
                curation_resolution_set_sha256=resolution_sha,
            )
            source_bytes = self._assert_source_packet_ref(event["source_packet_sha256"])
            require(
                event["source_packet_sha256"] == source_binding["source_packet_sha256"]
                and source_bytes == canonical_json_bytes(source_binding["source_packet"]),
                "ANNOTATION_LEDGER_INVALID",
                "annotation source packet differs from the active publication projection",
            )
            assignment_packet = self._assert_assignment_packet_ref(
                event["assignment_packet_sha256"]
            )
            from mobile_world.offline.gold_curation.server import _browser_packet

            source_packet = (
                self.publication.consistency_packet(event["unit_id"])
                if channel == "CONSISTENCY_AUDIT"
                else self.publication.packet(event["unit_id"], channel)
            )
            expected_assignment_packet = _browser_packet(
                source_packet,
                assignment_id=event["assignment_id"],
                role=role,
                reviewer_identity_sha256=self.identity_commitment(principal_id),
                source_binding=source_binding,
                compared_review_event_ids=compared_review_event_ids,
            )
            require(
                assignment_packet == expected_assignment_packet,
                "ANNOTATION_LEDGER_INVALID",
                "annotation assignment packet differs from its rederived blind projection",
            )

    def bind_assignment_packet(self, packet: Mapping[str, Any]) -> str:
        value = cast(dict[str, Any], json_copy(packet))
        validate_schema_record("curator_packet.schema.json", value)
        digest = cast(str, value["assignment_packet_sha256"])
        subject = {key: item for key, item in value.items() if key != "assignment_packet_sha256"}
        require(
            canonical_sha256(subject) == digest,
            "PACKET_BINDING_INVALID",
            "assignment packet digest differs",
        )
        packets = _ensure_child_directory(self.root, "assignment-packets")
        sha_root = _ensure_child_directory(packets, "sha256")
        prefix = _ensure_child_directory(sha_root, digest[:2])
        _write_once_regular(prefix / f"{digest}.json", canonical_json_bytes(value))
        return digest

    def _locked_append(
        self,
        *,
        event_kind: str,
        unit_id: str,
        channel: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        reviewer_identity_sha256: str,
        reviewer_role: str,
        payload: dict[str, Any],
        semantic_validator: Any,
    ) -> dict[str, Any]:
        self._assert_workspace_manifest()
        expected_source = self.bind_source_packet(unit_id, channel)
        require(
            source_packet_sha256 == expected_source["source_packet_sha256"],
            "PACKET_BINDING_INVALID",
            "annotation event source packet differs",
        )
        expected_assignment = self.assignment_id(
            unit_id,
            reviewer_role,
            channel=channel if reviewer_role == ADJUDICATOR_ROLE else None,
        )
        require(
            assignment_id == expected_assignment,
            "ASSIGNMENT_INVALID",
            "annotation event assignment differs",
        )
        for label, digest in (
            ("source packet", source_packet_sha256),
            ("assignment packet", assignment_packet_sha256),
        ):
            require(
                len(digest) == 64 and all(char in "0123456789abcdef" for char in digest),
                "PACKET_BINDING_INVALID",
                f"{label} digest is invalid",
            )
        self._assert_source_packet_ref(source_packet_sha256)
        assignment_packet = self._assert_assignment_packet_ref(assignment_packet_sha256)
        require(
            assignment_packet["assignment_id"] == assignment_id
            and assignment_packet["source_packet_sha256"] == source_packet_sha256
            and assignment_packet["reviewer_identity_sha256"] == reviewer_identity_sha256
            and assignment_packet["review_role"] == reviewer_role
            and assignment_packet["channel"] == channel,
            "PACKET_BINDING_INVALID",
            "annotation event differs from its assignment packet",
        )
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._journal, flags, 0o600)
        except OSError as exc:
            raise CurationError(
                "ANNOTATION_STORE_INVALID", "annotation ledger cannot be opened safely"
            ) from exc
        try:
            opened = os.fstat(fd)
            require(
                stat.S_ISREG(opened.st_mode)
                and opened.st_uid == os.geteuid()
                and opened.st_nlink == 1
                and opened.st_mode & 0o077 == 0,
                "ANNOTATION_STORE_INVALID",
                "annotation journal ownership, link count, or mode is unsafe",
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            events = self._decode_events(b"".join(chunks))
            self._validate_event_semantics(events)
            reused = semantic_validator(events)
            if isinstance(reused, dict):
                return cast(dict[str, Any], json_copy(reused))
            codec_gate_receipt_sha256 = (
                None if event_kind == "DRAFT_SAVED" else self._require_codec_gate()
            )
            subject = {
                "schema_version": ANNOTATION_EVENT_SCHEMA_VERSION,
                "record_type": "gold_curation_annotation_event",
                "event_id": "",
                "event_seq": len(events),
                "previous_event_sha256": events[-1]["event_sha256"] if events else None,
                "event_kind": event_kind,
                "created_at_ns": time.time_ns(),
                "unit_id": unit_id,
                "channel": channel,
                "assignment_id": assignment_id,
                "source_packet_sha256": source_packet_sha256,
                "assignment_packet_sha256": assignment_packet_sha256,
                "reviewer_identity_sha256": reviewer_identity_sha256,
                "reviewer_role": reviewer_role,
                "proposal_schema_version": REVIEW_PROPOSAL_SCHEMA_VERSION,
                "codec_gate_receipt_sha256": codec_gate_receipt_sha256,
                "payload": json_copy(payload),
                "payload_sha256": canonical_sha256(payload),
                "material_projection_sha256": None
                if event_kind == "DRAFT_SAVED"
                else canonical_sha256(
                    self.material_projection_for(
                        unit_id,
                        channel,
                        payload["resolved_payload"]
                        if event_kind == "ADJUDICATION_SUBMITTED"
                        else payload,
                    )
                ),
            }
            id_subject = {key: val for key, val in subject.items() if key != "event_id"}
            event = dict(subject)
            event["event_id"] = "g1annotation-" + canonical_sha256(id_subject)[:24]
            event["event_sha256"] = canonical_sha256(event)
            validate_schema_record("annotation_event.schema.json", event)
            validate_schema_record(
                "review_proposal.schema.json",
                event["payload"]["resolved_payload"]
                if event_kind == "ADJUDICATION_SUBMITTED"
                else event["payload"],
            )
            self._validate_event_semantics([*events, event], start_index=len(events))
            encoded = canonical_json_bytes(event)
            require(
                len(encoded) <= MAX_EVENT_BYTES,
                "ANNOTATION_EVENT_TOO_LARGE",
                "annotation event is too large",
            )
            _write_all(fd, encoded + b"\n")
            os.fsync(fd)
            return cast(dict[str, Any], json_copy(event))
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def assignment_id(self, unit_id: str, role: str, *, channel: str | None = None) -> str:
        validate_identity("placeholder", role)
        if role == ADJUDICATOR_ROLE:
            require(
                channel in CHANNELS,
                "CHANNEL_INVALID",
                "adjudicator assignment requires one channel",
            )
        else:
            require(
                channel is None, "CHANNEL_INVALID", "blind review assignment channel is role-bound"
            )
        require(
            any(item["unit_id"] == unit_id for item in self.publication.list_units()),
            "UNIT_UNKNOWN",
            "unit is not in the publication",
        )
        digest = hmac.new(
            self._assignment_key,
            (
                "mobileworld.g1.gold-curation.assignment/v1\0"
                f"{self.workspace_id}\0{channel or role_channel(role)}\0{role}\0{unit_id}"
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        return "g1assignment-" + digest[:32]

    def resolve_assignment(
        self, assignment_id: str, role: str, *, channel: str | None = None
    ) -> str:
        require(
            assignment_id.startswith("g1assignment-"),
            "ASSIGNMENT_INVALID",
            "assignment ID is invalid",
        )
        matches = [
            item["unit_id"]
            for item in self.publication.list_units()
            if hmac.compare_digest(
                self.assignment_id(item["unit_id"], role, channel=channel), assignment_id
            )
        ]
        require(
            len(matches) == 1, "ASSIGNMENT_INVALID", "assignment is not bound to the active role"
        )
        return cast(str, matches[0])

    def bind_source_packet(
        self,
        unit_id: str,
        channel: str,
    ) -> dict[str, Any]:
        curation_resolution_set_sha256 = (
            self.curation_resolution_set_sha256(unit_id) if channel == "CONSISTENCY_AUDIT" else None
        )
        binding = self.publication.source_packet_binding(
            unit_id,
            channel,
            curation_resolution_set_sha256=curation_resolution_set_sha256,
        )
        data = canonical_json_bytes(binding["source_packet"])
        digest = hashlib.sha256(data).hexdigest()
        require(
            digest == binding["source_packet_sha256"],
            "PACKET_BINDING_INVALID",
            "source packet digest differs",
        )
        packets = _ensure_child_directory(self.root, "packets")
        sha_root = _ensure_child_directory(packets, "sha256")
        prefix = _ensure_child_directory(sha_root, digest[:2])
        _write_once_regular(prefix / f"{digest}.json", data)
        return cast(dict[str, Any], json_copy(binding))

    def curation_resolution_set_sha256(self, unit_id: str) -> str:
        try:
            return self._resolution_set_sha256_from_events(self.read_events(), unit_id)
        except CurationError as exc:
            if exc.code == "ANNOTATION_LEDGER_INVALID":
                raise CurationError(
                    "CONSISTENCY_AUDIT_NOT_READY",
                    "formal curation channels are not both resolved",
                ) from exc
            raise

    def identity_commitment(self, reviewer_id: str) -> str:
        self.reviewer_registry.role_for(reviewer_id)
        return hmac.new(
            self._assignment_key,
            (
                f"mobileworld.g1.gold-curation.reviewer/v1\0{self.workspace_id}\0{reviewer_id}"
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

    def assert_identity_role(self, reviewer_id: str, reviewer_role: str) -> str:
        validate_identity(reviewer_id, reviewer_role)
        require(
            self.reviewer_registry.role_for(reviewer_id) == reviewer_role,
            "REVIEWER_AUTHENTICATION_FAILED",
            "reviewer role differs from the owner registry",
        )
        commitment = self.identity_commitment(reviewer_id)
        return commitment

    def authenticate_identity(
        self, reviewer_id: Any, reviewer_role: Any, access_secret: Any
    ) -> tuple[str, str, str]:
        principal, role = self.reviewer_registry.authenticate(
            reviewer_id, reviewer_role, access_secret
        )
        return principal, role, self.assert_identity_role(principal, role)

    def adjudicator_channel_for(self, reviewer_id: str) -> str:
        self.assert_identity_role(reviewer_id, ADJUDICATOR_ROLE)
        return self.reviewer_registry.adjudication_channel_for(reviewer_id)

    @staticmethod
    def _identity_guard(
        events: list[dict[str, Any]], reviewer_identity_sha256: str, reviewer_role: str
    ) -> None:
        roles = {
            event["reviewer_role"]
            for event in events
            if event["reviewer_identity_sha256"] == reviewer_identity_sha256
        }
        require(
            not roles or roles == {reviewer_role},
            "REVIEWER_ROLE_COLLISION",
            "one identity cannot cross reviewer roles or channels",
        )

    def save_draft(
        self,
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        require(
            reviewer_role in REVIEW_ROLES,
            "ROLE_INVALID",
            "adjudicator cannot save a blind-review draft",
        )
        channel = role_channel(reviewer_role)
        unit = next(
            (item for item in self.publication.list_units() if item["unit_id"] == unit_id), None
        )
        require(unit is not None, "UNIT_UNKNOWN", "unit is not in the publication")
        unit = cast(dict[str, Any], unit)
        validated = validate_review_payload(
            channel, payload, clean_control=unit["unit_kind"] == "CLEAN_CONTROL"
        )
        self.publication.validate_review_payload_binding(unit_id, channel, validated)
        if channel == "CONSISTENCY_AUDIT":
            require(
                self.consistency_ready(unit_id),
                "CONSISTENCY_AUDIT_NOT_READY",
                "consistency audit opens only after gold/transformation resolution",
            )

        def guard(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            self._identity_guard(events, reviewer_identity_sha256, reviewer_role)
            require(
                not any(
                    event["event_kind"] == "REVIEW_SUBMITTED"
                    and event["unit_id"] == unit_id
                    and event["reviewer_role"] == reviewer_role
                    for event in events
                ),
                "REVIEW_ALREADY_SUBMITTED",
                "submitted review is immutable",
            )
            return None

        return self._locked_append(
            event_kind="DRAFT_SAVED",
            unit_id=unit_id,
            channel=channel,
            assignment_id=assignment_id,
            source_packet_sha256=source_packet_sha256,
            assignment_packet_sha256=assignment_packet_sha256,
            reviewer_identity_sha256=reviewer_identity_sha256,
            reviewer_role=reviewer_role,
            payload=validated,
            semantic_validator=guard,
        )

    def submit_review(
        self,
        *,
        unit_id: str,
        reviewer_id: str,
        reviewer_role: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        require(reviewer_role in REVIEW_ROLES, "ROLE_INVALID", "role cannot submit a blind review")
        channel = role_channel(reviewer_role)
        unit = next(
            (item for item in self.publication.list_units() if item["unit_id"] == unit_id), None
        )
        require(unit is not None, "UNIT_UNKNOWN", "unit is not in the publication")
        unit = cast(dict[str, Any], unit)
        if channel == "CONSISTENCY_AUDIT":
            require(
                self.consistency_ready(unit_id),
                "CONSISTENCY_AUDIT_NOT_READY",
                "consistency audit opens only after gold/transformation resolution",
            )
        self._require_codec_gate()
        validated = validate_review_payload(
            channel, payload, clean_control=unit["unit_kind"] == "CLEAN_CONTROL"
        )
        self.publication.validate_review_payload_binding(unit_id, channel, validated)

        def guard(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            self._identity_guard(events, reviewer_identity_sha256, reviewer_role)
            existing = [
                event
                for event in events
                if event["event_kind"] == "REVIEW_SUBMITTED"
                and event["unit_id"] == unit_id
                and event["reviewer_role"] == reviewer_role
            ]
            if existing:
                require(
                    len(existing) == 1
                    and existing[0]["payload"] == validated
                    and existing[0]["assignment_id"] == assignment_id
                    and existing[0]["source_packet_sha256"] == source_packet_sha256
                    and existing[0]["assignment_packet_sha256"] == assignment_packet_sha256,
                    "REVIEW_ALREADY_SUBMITTED",
                    "review role already finalized different bytes",
                )
                return existing[0]
            counterpart = reviewer_role.rsplit("_", 1)[0] + (
                "_SECONDARY" if reviewer_role.endswith("_PRIMARY") else "_PRIMARY"
            )
            identities = {
                event["reviewer_identity_sha256"]
                for event in events
                if event["event_kind"] == "REVIEW_SUBMITTED"
                and event["unit_id"] == unit_id
                and event["reviewer_role"] == counterpart
            }
            require(
                reviewer_identity_sha256 not in identities,
                "REVIEWER_INDEPENDENCE_VIOLATION",
                "primary and secondary must be different people",
            )
            if channel == "CONSISTENCY_AUDIT":
                require(
                    self._channels_resolved(events, unit_id, ("ACTION_GOLD", "TRANSFORMATION")),
                    "CONSISTENCY_AUDIT_NOT_READY",
                    "consistency audit opens only after gold/transformation resolution",
                )
            return None

        return self._locked_append(
            event_kind="REVIEW_SUBMITTED",
            unit_id=unit_id,
            channel=channel,
            assignment_id=assignment_id,
            source_packet_sha256=source_packet_sha256,
            assignment_packet_sha256=assignment_packet_sha256,
            reviewer_identity_sha256=reviewer_identity_sha256,
            reviewer_role=reviewer_role,
            payload=validated,
            semantic_validator=guard,
        )

    @staticmethod
    def _final_reviews(
        events: list[dict[str, Any]], unit_id: str, channel: str
    ) -> dict[str, dict[str, Any]]:
        prefix = channel + "_"
        return {
            event["reviewer_role"].removeprefix(prefix): event
            for event in events
            if event["event_kind"] == "REVIEW_SUBMITTED"
            and event["unit_id"] == unit_id
            and event["channel"] == channel
        }

    def material_projection_for(
        self, unit_id: str, channel: str, payload: Mapping[str, Any]
    ) -> Any:
        return material_projection(
            channel,
            payload,
            record_bindings=self.publication.record_bindings(unit_id),
        )

    def disagreement_fields_for(
        self,
        unit_id: str,
        channel: str,
        primary: Mapping[str, Any],
        secondary: Mapping[str, Any],
    ) -> list[str]:
        return disagreement_fields(
            channel,
            primary,
            secondary,
            record_bindings=self.publication.record_bindings(unit_id),
        )

    def _channel_resolution(
        self, events: list[dict[str, Any]], unit_id: str, channel: str
    ) -> dict[str, Any] | None:
        reviews = self._final_reviews(events, unit_id, channel)
        if set(reviews) != {"PRIMARY", "SECONDARY"}:
            return None
        primary = reviews["PRIMARY"]["payload"]
        secondary = reviews["SECONDARY"]["payload"]
        differences = self.disagreement_fields_for(unit_id, channel, primary, secondary)
        if not differences:
            return {
                "resolution_kind": "INDEPENDENT_AGREEMENT",
                "payload": json_copy(primary),
                "disagreement_fields": [],
                "review_event_ids": [
                    reviews["PRIMARY"]["event_id"],
                    reviews["SECONDARY"]["event_id"],
                ],
            }
        adjudications = [
            event
            for event in events
            if event["event_kind"] == "ADJUDICATION_SUBMITTED"
            and event["unit_id"] == unit_id
            and event["channel"] == channel
        ]
        if len(adjudications) != 1:
            return None
        event = adjudications[0]
        return {
            "resolution_kind": "ADJUDICATED",
            "payload": json_copy(event["payload"]["resolved_payload"]),
            "disagreement_fields": list(event["payload"]["disagreement_fields"]),
            "review_event_ids": [reviews["PRIMARY"]["event_id"], reviews["SECONDARY"]["event_id"]],
            "adjudication_event_id": event["event_id"],
        }

    def _channels_resolved(
        self, events: list[dict[str, Any]], unit_id: str, channels: tuple[str, ...]
    ) -> bool:
        return all(
            self._channel_resolution(events, unit_id, channel) is not None for channel in channels
        )

    def submit_adjudication(
        self,
        *,
        unit_id: str,
        channel: str,
        reviewer_id: str,
        assignment_id: str,
        source_packet_sha256: str,
        assignment_packet_sha256: str,
        resolved_payload: Mapping[str, Any],
        rationale: str,
    ) -> dict[str, Any]:
        self._require_codec_gate()
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, ADJUDICATOR_ROLE)
        require(channel in CHANNELS, "CHANNEL_INVALID", "adjudication channel is invalid")
        require(
            channel == self.reviewer_registry.adjudication_channel_for(reviewer_id),
            "REVIEWER_ROLE_COLLISION",
            "adjudicator is owner-bound to a different channel",
        )
        unit = next(
            (item for item in self.publication.list_units() if item["unit_id"] == unit_id), None
        )
        require(unit is not None, "UNIT_UNKNOWN", "unit is not in the publication")
        unit = cast(dict[str, Any], unit)
        validated = validate_review_payload(
            channel, resolved_payload, clean_control=unit["unit_kind"] == "CLEAN_CONTROL"
        )
        self.publication.validate_review_payload_binding(unit_id, channel, validated)
        require(
            bool(rationale.strip()),
            "ADJUDICATION_INVALID",
            "adjudication rationale is required",
        )

        def guard(events: list[dict[str, Any]]) -> dict[str, Any] | None:
            self._identity_guard(events, reviewer_identity_sha256, ADJUDICATOR_ROLE)
            reviews = self._final_reviews(events, unit_id, channel)
            require(
                set(reviews) == {"PRIMARY", "SECONDARY"},
                "ADJUDICATION_NOT_READY",
                "two final reviews are required",
            )
            reviewer_ids = {event["reviewer_identity_sha256"] for event in reviews.values()}
            require(
                reviewer_identity_sha256 not in reviewer_ids,
                "REVIEWER_INDEPENDENCE_VIOLATION",
                "adjudicator must be identity-disjoint",
            )
            differences = self.disagreement_fields_for(
                unit_id,
                channel,
                reviews["PRIMARY"]["payload"],
                reviews["SECONDARY"]["payload"],
            )
            require(
                bool(differences),
                "ADJUDICATION_NOT_REQUIRED",
                "matching reviews do not require adjudication",
            )
            existing = [
                event
                for event in events
                if event["event_kind"] == "ADJUDICATION_SUBMITTED"
                and event["unit_id"] == unit_id
                and event["channel"] == channel
            ]
            if existing:
                require(
                    len(existing) == 1
                    and existing[0]["payload"] == payload
                    and existing[0]["assignment_id"] == assignment_id
                    and existing[0]["source_packet_sha256"] == source_packet_sha256
                    and existing[0]["assignment_packet_sha256"] == assignment_packet_sha256,
                    "ADJUDICATION_ALREADY_SUBMITTED",
                    "adjudication already finalized different bytes",
                )
                return existing[0]
            return None

        current = self.read_events()
        reviews = self._final_reviews(current, unit_id, channel)
        require(
            set(reviews) == {"PRIMARY", "SECONDARY"},
            "ADJUDICATION_NOT_READY",
            "two final reviews are required",
        )
        differences = self.disagreement_fields_for(
            unit_id,
            channel,
            reviews["PRIMARY"]["payload"],
            reviews["SECONDARY"]["payload"],
        )
        payload = {
            "disagreement_fields": differences,
            "primary_material_projection_sha256": canonical_sha256(
                self.material_projection_for(unit_id, channel, reviews["PRIMARY"]["payload"])
            ),
            "secondary_material_projection_sha256": canonical_sha256(
                self.material_projection_for(unit_id, channel, reviews["SECONDARY"]["payload"])
            ),
            "resolved_payload": validated,
            "rationale": rationale,
            "primary_event_id": reviews["PRIMARY"]["event_id"],
            "secondary_event_id": reviews["SECONDARY"]["event_id"],
        }
        return self._locked_append(
            event_kind="ADJUDICATION_SUBMITTED",
            unit_id=unit_id,
            channel=channel,
            assignment_id=assignment_id,
            source_packet_sha256=source_packet_sha256,
            assignment_packet_sha256=assignment_packet_sha256,
            reviewer_identity_sha256=reviewer_identity_sha256,
            reviewer_role=ADJUDICATOR_ROLE,
            payload=payload,
            semantic_validator=guard,
        )

    def latest_draft(
        self, unit_id: str, reviewer_id: str, reviewer_role: str
    ) -> dict[str, Any] | None:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, reviewer_role)
        drafts = [
            event
            for event in self.read_events()
            if event["event_kind"] == "DRAFT_SAVED"
            and event["unit_id"] == unit_id
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            and event["reviewer_role"] == reviewer_role
        ]
        return None if not drafts else json_copy(drafts[-1]["payload"])

    def channel_resolution(self, unit_id: str, channel: str) -> dict[str, Any] | None:
        require(channel in CHANNELS, "CHANNEL_INVALID", "channel is invalid")
        return self._channel_resolution(self.read_events(), unit_id, channel)

    def consistency_ready(self, unit_id: str) -> bool:
        return self._channels_resolved(
            self.read_events(), unit_id, ("ACTION_GOLD", "TRANSFORMATION")
        )

    def adjudication_case(self, unit_id: str, channel: str, reviewer_id: str) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, ADJUDICATOR_ROLE)
        require(
            channel == self.reviewer_registry.adjudication_channel_for(reviewer_id),
            "REVIEWER_ROLE_COLLISION",
            "adjudicator is owner-bound to a different channel",
        )
        events = self.read_events()
        self._identity_guard(events, reviewer_identity_sha256, ADJUDICATOR_ROLE)
        reviews = self._final_reviews(events, unit_id, channel)
        require(
            set(reviews) == {"PRIMARY", "SECONDARY"},
            "ADJUDICATION_NOT_READY",
            "two final reviews are required",
        )
        require(
            reviewer_identity_sha256
            not in {item["reviewer_identity_sha256"] for item in reviews.values()},
            "REVIEWER_INDEPENDENCE_VIOLATION",
            "adjudicator must be independent",
        )
        differences = self.disagreement_fields_for(
            unit_id,
            channel,
            reviews["PRIMARY"]["payload"],
            reviews["SECONDARY"]["payload"],
        )
        require(
            bool(differences), "ADJUDICATION_NOT_REQUIRED", "reviews do not materially disagree"
        )
        return {
            "unit_id": unit_id,
            "channel": channel,
            "disagreement_fields": differences,
            "primary": {
                "reviewer_identity_sha256": reviews["PRIMARY"]["reviewer_identity_sha256"],
                "event_id": reviews["PRIMARY"]["event_id"],
                "payload": json_copy(reviews["PRIMARY"]["payload"]),
            },
            "secondary": {
                "reviewer_identity_sha256": reviews["SECONDARY"]["reviewer_identity_sha256"],
                "event_id": reviews["SECONDARY"]["event_id"],
                "payload": json_copy(reviews["SECONDARY"]["payload"]),
            },
        }

    def status_for(self, unit_id: str, role: str, reviewer_id: str) -> dict[str, Any]:
        reviewer_identity_sha256 = self.assert_identity_role(reviewer_id, role)
        events = self.read_events()
        if role == ADJUDICATOR_ROLE:
            pending: list[str] = []
            for channel in (self.reviewer_registry.adjudication_channel_for(reviewer_id),):
                reviews = self._final_reviews(events, unit_id, channel)
                if set(reviews) == {"PRIMARY", "SECONDARY"} and self.disagreement_fields_for(
                    unit_id,
                    channel,
                    reviews["PRIMARY"]["payload"],
                    reviews["SECONDARY"]["payload"],
                ):
                    if self._channel_resolution(events, unit_id, channel) is None:
                        pending.append(channel)
            workflow_state = "ADJUDICATION_REQUIRED" if pending else "RESOLVED"
            state = "ADJUDICATING" if pending else "RESOLVED"
            return {
                "state": state,
                "own_state": "NOT_ASSIGNED",
                "workflow_state": workflow_state,
                "can_open": bool(pending),
                "channels": pending,
            }
        channel = role_channel(role)
        own_submitted = any(
            event["event_kind"] == "REVIEW_SUBMITTED"
            and event["unit_id"] == unit_id
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            and event["reviewer_role"] == role
            for event in events
        )
        drafted = any(
            event["event_kind"] == "DRAFT_SAVED"
            and event["unit_id"] == unit_id
            and event["reviewer_identity_sha256"] == reviewer_identity_sha256
            and event["reviewer_role"] == role
            for event in events
        )
        own_state = "FINALIZED" if own_submitted else "DRAFTING" if drafted else "NOT_ASSIGNED"
        if channel == "CONSISTENCY_AUDIT" and not self._channels_resolved(
            events, unit_id, ("ACTION_GOLD", "TRANSFORMATION")
        ):
            return {
                "state": "WAITING_FOR_PEER",
                "own_state": own_state,
                "workflow_state": "WAITING_FOR_PEER",
                "can_open": False,
            }
        resolution = self._channel_resolution(events, unit_id, channel)
        if resolution is not None:
            workflow_state = "RESOLVED"
        else:
            reviews = self._final_reviews(events, unit_id, channel)
            if set(reviews) == {"PRIMARY", "SECONDARY"}:
                workflow_state = "ADJUDICATION_REQUIRED"
            elif reviews:
                workflow_state = "WAITING_FOR_PEER"
            else:
                workflow_state = own_state
        state = (
            workflow_state
            if workflow_state in {"WAITING_FOR_PEER", "ADJUDICATION_REQUIRED", "RESOLVED"}
            else own_state
        )
        return {
            "state": state,
            "own_state": own_state,
            "workflow_state": workflow_state,
            "can_open": not own_submitted and resolution is None,
        }

    def export_workspace_receipt(self) -> dict[str, Any]:
        events = self.read_events()
        codec_gate_receipt_sha256 = self._require_codec_gate()
        resolutions: list[dict[str, Any]] = []
        for unit in self.publication.list_units():
            unit_id = unit["unit_id"]
            for channel in CHANNELS:
                resolution = self._channel_resolution(events, unit_id, channel)
                if resolution is not None:
                    resolutions.append({"unit_id": unit_id, "channel": channel, **resolution})
        return {
            "schema_version": "mobileworld.g1.gold-curation-workspace-export/v1",
            "record_type": "gold_curation_workspace_export",
            "publication_manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
            "codec_gate_receipt_sha256": codec_gate_receipt_sha256,
            "event_count": len(events),
            "last_event_sha256": events[-1]["event_sha256"] if events else None,
            "resolved_channel_count": len(resolutions),
            "resolutions": resolutions,
            "formal_g1_6_bundle": False,
            "admission_ready": False,
            "execution_ready": False,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
            "gpu_used": False,
            "model_invoked": False,
            "formal_replay_performed": False,
        }
