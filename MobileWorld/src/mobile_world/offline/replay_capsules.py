"""Materialize immutable G1.3 replay capsules from frozen Collector v1 facts.

This is a CPU-only, offline derived-data consumer.  It does not invoke a
provider, restore an emulator, execute an action, select an intervention, or
infer whether any historical claim is true.  The post-request natural
response/action/transition are retained only in a structurally sealed audit
section and are never part of the renderer or curator projections.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, RefResolver

from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.schemas import validate_event_envelope
from mobile_world.runtime.audit.serializer import (
    ARTIFACT_GRAPH_VERSION,
    ArtifactSerializer,
    canonical_json_bytes,
)

PROTOCOL_VERSION = "mobileworld.g1.causal-replay/protocol-v1"
PORTABLE_CONTRACT_VERSION = "mobileworld.g1.portable-sentinel/contract-v1"
CONTRACT_AMENDMENT_VERSION = "mobileworld.g1.replay-capsule/contract-v1-amendment-1"
BUILDER_VERSION = "mobileworld.g1.replay-capsule-builder/v1.1"
CAPSULE_SCHEMA_VERSION = "mobileworld.g1.replay-capsule/v1.1"
MANIFEST_SCHEMA_VERSION = "mobileworld.g1.replay-capsule-manifest/v1.1"
INTEGRITY_SCHEMA_VERSION = "mobileworld.g1.replay-capsule-integrity/v1.1"
EXCLUSION_SCHEMA_VERSION = "mobileworld.g1.replay-capsule-exclusion/v1"
VISIBILITY_SCHEMA_VERSION = "mobileworld.g1.replay-capsule.field-visibility/v1"

LEGACY_CAPSULE_SCHEMA_VERSION = "mobileworld.g1.replay-capsule/v1"
LEGACY_MANIFEST_SCHEMA_VERSION = "mobileworld.g1.replay-capsule-manifest/v1"
LEGACY_INTEGRITY_SCHEMA_VERSION = "mobileworld.g1.replay-capsule-integrity/v1"

G1_REGISTRY_LOCK_SHA256 = "1e038ffe604acf0eae2af1e45ec0e856e2f105353b0c5a1dbea0da9b15657944"
G1_REGISTRY_MANIFEST_SHA256 = "dd3dad4f94c66dce6999d3cc2743cd75c37688788754e95b27531cfd00d733f4"
G1_REGISTRY_AGGREGATE_SHA256 = "dbec86f012b1cb9a11f94123cb302a62ffc6a04a33422121d190f28edf793bc6"
G1_CONTRACT_AGGREGATE_SHA256 = "f1e23239896eb7f6487e337ec391df73d19c84fababecae996c0a2e752f156d8"
G1_SOURCE_CONFIG_SHA256 = "c8235705c575e134c11bc00896f31ec95243af4ffd2ffd47a3e6ecf64ce5cb59"
MODEL_MANIFEST_SHA256 = "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f"

TARGET_POPULATION = 190
STRICT_TARGET_COUNT = 152
SELECTED_CLEAN_TARGET_COUNT = 38
RESERVE_CONTROL_COUNT = 38
EXPECTED_MODEL_COUNTS = {"qwen3vl_8b": 169, "mai_ui_8b": 21}

BASE_OUTPUT_FILE_NAMES = frozenset(
    {
        "capsule_index.jsonl",
        "capsule_exclusions.jsonl",
        "field_visibility.json",
        "capsule_integrity.json",
        "capsule_manifest.json",
    }
)

CONTRACT_RELATIVE_PATHS = (
    "MobileWorld/scripts/build_g1_replay_capsules.py",
    "MobileWorld/src/mobile_world/offline/replay_capsules.py",
    "MobileWorld/tests/offline/test_replay_capsules.py",
    "mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1.md",
    "mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md",
    "mobileworld_audit_handoff/DECISION_LOG.md",
    "mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md",
    "mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md",
    "mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md",
    "mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
    "mobileworld_audit_handoff/g1/registry.lock.v1.json",
    "mobileworld_audit_handoff/schemas/g1_3/replay_capsule.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/replay_capsule.v1_1.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.v1_1.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.v1_1.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/capsule_exclusion.schema.json",
    "mobileworld_audit_handoff/schemas/g1_3/field_visibility.schema.json",
)

VISIBILITY_CLASSES = (
    "FROZEN_MODEL_VISIBLE",
    "FROZEN_NON_HISTORY_ENVELOPE",
    "MUTABLE_HISTORY_TREATMENT",
    "CURATOR_ONLY",
    "POST_ACTION_AUDIT_ONLY",
)

SEMANTIC_REQUEST_VISIBILITY_CLASSES = (
    "FROZEN_MODEL_VISIBLE",
    "FROZEN_NON_HISTORY_ENVELOPE",
    "MUTABLE_HISTORY_TREATMENT",
)

EXCLUSION_CODES = frozenset(
    {
        "REGISTRY_BINDING_INVALID",
        "SOURCE_REFERENCE_UNRESOLVED",
        "SOURCE_HASH_MISMATCH",
        "RAW_EVENT_CHAIN_INVALID",
        "BLOB_REFERENCE_INVALID",
        "BLOB_MISSING",
        "BLOB_HASH_MISMATCH",
        "ARTIFACT_REHYDRATION_FAILED",
        "REQUEST_HASH_MISMATCH",
        "REQUEST_VIEW_MISMATCH",
        "REQUEST_PARTITION_AMBIGUOUS",
        "REQUEST_PARTITION_INCOMPLETE",
        "NON_HISTORY_REGION_UNRECOVERABLE",
        "STATE_HASH_MISMATCH",
        "CURRENT_OBSERVATION_UNRESOLVED",
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "TARGET_SPAN_UNRESOLVED",
        "TARGET_SPAN_AMBIGUOUS",
        "TARGET_SPAN_HASH_MISMATCH",
        "TARGET_SPAN_COORDINATE_MISMATCH",
        "TARGET_SET_OVERLAP",
        "ORIGINAL_RESPONSE_UNRESOLVED",
        "ORIGINAL_ACTION_UNRESOLVED",
        "ORIGINAL_TRANSITION_UNRESOLVED",
        "BACKEND_DEPENDENCY_UNPROVEN",
        "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING",
        "PREFIX_REPLAY_RECIPE_INVALID",
        "FUTURE_EVIDENCE_LEAKAGE",
        "FIELD_VISIBILITY_INVALID",
        "CURATOR_CHANNEL_VIOLATION",
        "SCHEMA_VALIDATION_FAILED",
        "DUPLICATE_CAPSULE",
        "CAPSULE_HASH_MISMATCH",
        "NONDETERMINISTIC_BUILD",
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_QWEN_RECORD_PATH_RE = re.compile(
    r"^payload\.request_view\.messages\[(?P<message>\d+)\]\.content"
    r"\[(?P<block>\d+)\]\.text$"
)
_RAW_RECORD_PATH_RE = re.compile(r"^payload\.request_view\.messages\[(?P<message>\d+)\]\.content$")
_BLOB_KEYS = frozenset({"algorithm", "digest", "byte_length", "media_type", "relative_path"})


class ReplayCapsuleError(RuntimeError):
    """One stable, machine-readable G1.3 materialization failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "MATERIALIZE",
        json_path: str = "$",
        **context: Any,
    ) -> None:
        super().__init__(message)
        if code not in EXCLUSION_CODES:
            code = "SCHEMA_VALIDATION_FAILED"
        self.code = code
        self.stage = stage
        self.json_path = json_path
        self.context = context


@dataclass(frozen=True, slots=True)
class RegistryUnit:
    """One canonical G1.1 target row plus its immutable line identity."""

    unit_kind: str
    unit_id: str
    registry_file: str
    registry_file_sha256: str
    registry_file_byte_count: int
    line_number: int
    line_sha256: str
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EventStream:
    """Canonical Collector v1 task stream with exact line identities."""

    relative_path: str
    sha256: str
    byte_count: int
    events: tuple[dict[str, Any], ...]
    line_sha256_by_id: Mapping[str, str]
    event_by_id: Mapping[str, dict[str, Any]]


@dataclass(slots=True)
class DerivedArtifactSink:
    """Collect deterministic, flat, content-addressed derived artifacts."""

    payloads: dict[str, bytes]

    def put_json(self, value: Any, *, media_type: str) -> dict[str, Any]:
        return self.put_bytes(canonical_json_bytes(value), media_type=media_type)

    def put_text(self, value: str, *, media_type: str) -> dict[str, Any]:
        return self.put_bytes(value.encode("utf-8"), media_type=media_type)

    def put_bytes(self, data: bytes, *, media_type: str) -> dict[str, Any]:
        digest = sha256_bytes(data)
        name = f"artifact-{digest}"
        existing = self.payloads.get(name)
        _require(
            existing is None or existing == data,
            "CAPSULE_HASH_MISMATCH",
            "derived artifact digest collision",
            stage="DERIVED_ARTIFACT",
        )
        self.payloads[name] = data
        return {
            "store_id": "G1_3_PUBLICATION",
            "relative_path": name,
            "sha256": digest,
            "byte_count": len(data),
            "media_type": media_type,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def g1_1_canonical_sha256(value: Any) -> str:
    """Hash a frozen G1.1 canonical JSONL record, including its final LF."""

    return sha256_bytes(canonical_json_line(value))


def canonical_json_line(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def build_capsule_artifacts(
    *,
    repo_root: str | os.PathLike[str],
    registry_root: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
) -> dict[str, Any]:
    """Build one truthful, non-publishable G1.3 candidate in memory."""

    repo = Path(repo_root).resolve(strict=True)
    registry = Path(registry_root).resolve(strict=True)
    sources_root = Path(source_base).resolve(strict=True)
    frozen = _load_frozen_inputs(repo, registry)
    visibility = field_visibility_policy()
    visibility_sha256 = canonical_sha256(visibility)
    derived_payloads: dict[str, bytes] = {}

    source_config = frozen["source_config"]
    source_by_key = {record["source_key"]: record for record in source_config["sources"]}
    model_manifest = frozen["model_manifest"]
    model_by_id = {record["model_id"]: record for record in model_manifest["models"]}
    source_snapshot_before = _snapshot_population_source_files(
        population=frozen["population"],
        source_by_key=source_by_key,
        source_base=sources_root,
    )
    stream_cache: dict[
        tuple[str, str], tuple[Path, EventStream, dict[str, Any], dict[str, Any]]
    ] = {}

    capsules: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for unit in frozen["population"]:
        unit_payloads: dict[str, bytes] = {}
        try:
            capsule = _materialize_capsule(
                unit=unit,
                repo_root=repo,
                source_base=sources_root,
                source_spec=source_by_key[unit.record["source_key"]],
                model_record=model_by_id[unit.record["model_id"]],
                visibility_sha256=visibility_sha256,
                stream_cache=stream_cache,
                sink=DerivedArtifactSink(unit_payloads),
            )
            for name, data in unit_payloads.items():
                existing = derived_payloads.get(name)
                _require(
                    existing is None or existing == data,
                    "CAPSULE_HASH_MISMATCH",
                    "derived artifact digest collision across units",
                    stage="DERIVED_ARTIFACT",
                )
                derived_payloads[name] = data
            capsules.append(capsule)
        except ReplayCapsuleError as error:
            exclusions.append(_exclusion_for(unit, error))

    capsules.sort(
        key=lambda record: (
            record["capsule"]["unit"]["unit_kind"],
            record["capsule"]["unit"]["unit_id"],
        )
    )
    exclusions.sort(
        key=lambda record: (
            record["unit_id"],
            record["primary_stage"],
            record["primary_reason_code"],
        )
    )
    _require(
        len(capsules) + len(exclusions) == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "target population disposition count changed",
        stage="POPULATION",
    )
    _require(
        len({record["capsule"]["unit"]["unit_id"] for record in capsules}) == len(capsules),
        "DUPLICATE_CAPSULE",
        "capsule unit identity collision",
        stage="POPULATION",
    )

    capsule_file_refs: list[dict[str, Any]] = []
    index_by_unit: dict[str, dict[str, Any]] = {}
    for record in capsules:
        unit_id = record["capsule"]["unit"]["unit_id"]
        data = canonical_json_line(record)
        name = f"capsule-{unit_id}.json"
        derived_payloads[name] = data
        file_ref = _file_summary(data, name)
        capsule_file_refs.append(file_ref)
        index_by_unit[unit_id] = {
            "unit_kind": record["capsule"]["unit"]["unit_kind"],
            "unit_id": unit_id,
            "registry_record_ref": _manifest_registry_record_ref(
                record["capsule"]["unit"]["registry_record_ref"]
            ),
            "source_key": record["capsule"]["unit"]["source_key"],
            "model_id": record["capsule"]["unit"]["model_id"],
            "history_family": record["capsule"]["unit"]["history_family"],
            "disposition": "CAPSULED",
            "capsule_ref": file_ref,
            "capsule_body_sha256": record["capsule_body_sha256"],
            "exclusion_record_index": None,
            "exclusion_record_sha256": None,
        }
    exclusion_bytes = b"".join(canonical_json_line(record) for record in exclusions)
    exclusion_ledger_ref = _file_summary(exclusion_bytes, "capsule_exclusions.jsonl")
    for record_index, record in enumerate(exclusions):
        unit = next(unit for unit in frozen["population"] if unit.unit_id == record["unit_id"])
        line_sha256 = sha256_bytes(canonical_json_line(record))
        index_by_unit[unit.unit_id] = {
            "unit_kind": unit.unit_kind,
            "unit_id": unit.unit_id,
            "registry_record_ref": _manifest_registry_record_ref(_registry_record_ref(unit)),
            "source_key": unit.record["source_key"],
            "model_id": unit.record["model_id"],
            "history_family": unit.record["history_family"].lower(),
            "disposition": "EXCLUDED",
            "capsule_ref": None,
            "capsule_body_sha256": None,
            "exclusion_record_index": record_index,
            "exclusion_record_sha256": line_sha256,
        }
    _require(
        len(index_by_unit) == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "manifest disposition index does not cover all target units",
        stage="POPULATION",
    )
    capsule_index = sorted(index_by_unit.values(), key=_manifest_unit_sort_key)
    capsule_set_subject = [
        {
            "unit_kind": item["unit_kind"],
            "unit_id": item["unit_id"],
            "disposition": item["disposition"],
            "capsule_body_sha256": item["capsule_body_sha256"],
            "exclusion_record_sha256": item["exclusion_record_sha256"],
        }
        for item in capsule_index
    ]
    capsule_set_sha256 = canonical_sha256(capsule_set_subject)
    capsule_index_bytes = b"".join(canonical_json_line(record) for record in capsule_index)
    visibility_bytes = canonical_json_line(visibility)
    contract_files = _contract_file_summaries(repo)
    _verify_capsule_source_closure(capsules, repo_root=repo, source_base=sources_root)
    source_snapshot_after = _snapshot_population_source_files(
        population=frozen["population"],
        source_by_key=source_by_key,
        source_base=sources_root,
    )
    _require(
        source_snapshot_before == source_snapshot_after,
        "SOURCE_HASH_MISMATCH",
        "referenced Collector source closure changed during candidate build",
        stage="SOURCE",
    )
    counts = _population_counts(capsules, exclusions)
    integrity = _build_integrity_report(
        phase="BUILD_CANDIDATE",
        capsules=capsules,
        exclusions=exclusions,
        capsule_set_sha256=capsule_set_sha256,
        capsule_index=capsule_index,
        exclusion_ledger_ref=exclusion_ledger_ref,
        source_snapshot_before=source_snapshot_before,
        source_snapshot_after=source_snapshot_after,
        double_build=_candidate_double_build_receipt(),
        counts=counts,
    )
    integrity_bytes = canonical_json_line(integrity)
    payloads: dict[str, bytes] = {
        **derived_payloads,
        "capsule_index.jsonl": capsule_index_bytes,
        "capsule_exclusions.jsonl": exclusion_bytes,
        "field_visibility.json": visibility_bytes,
        "capsule_integrity.json": integrity_bytes,
    }
    manifest = _build_manifest(
        repo=repo,
        phase="BUILD_CANDIDATE",
        contract_files=contract_files,
        capsule_set_sha256=capsule_set_sha256,
        capsule_index=capsule_index,
        capsule_file_refs=capsule_file_refs,
        counts=counts,
        payloads=payloads,
    )
    manifest_bytes = canonical_json_line(manifest)
    payloads["capsule_manifest.json"] = manifest_bytes
    artifacts = {
        "capsules": capsules,
        "exclusions": exclusions,
        "visibility": visibility,
        "integrity": integrity,
        "manifest": manifest,
        "file_payloads": payloads,
    }
    _validate_artifacts_against_schemas(repo, artifacts)
    return artifacts


def build_verified_capsule_artifacts(
    *,
    repo_root: str | os.PathLike[str],
    registry_root: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
) -> dict[str, Any]:
    """Independently build twice and return one byte-identical formal set."""

    arguments = {
        "repo_root": repo_root,
        "registry_root": registry_root,
        "source_base": source_base,
    }
    first = build_capsule_artifacts(**arguments)
    second = build_capsule_artifacts(**arguments)
    first_core = _core_file_payloads(first["file_payloads"])
    second_core = _core_file_payloads(second["file_payloads"])
    _require(
        set(first_core) == set(second_core)
        and all(first_core[name] == second_core[name] for name in first_core),
        "NONDETERMINISTIC_BUILD",
        "independent core capsule builds are not byte-identical",
        stage="DETERMINISM",
    )
    _require(
        not first["exclusions"]
        and not second["exclusions"]
        and len(first["capsules"]) == TARGET_POPULATION
        and len(second["capsules"]) == TARGET_POPULATION,
        "SCHEMA_VALIDATION_FAILED",
        "formal publication requires all 190 target units to be capsuled",
        stage="SCHEMA",
    )
    core_sha256 = _file_set_aggregate(first_core)
    receipt = {
        "status": "PASSED",
        "performed": True,
        "comparison_scope": "CORE_FILES_EXCLUDING_INTEGRITY_AND_MANIFEST",
        "first_core_file_set_sha256": core_sha256,
        "second_core_file_set_sha256": _file_set_aggregate(second_core),
        "output_file_set_bytes_identical": True,
        "manifest_bytes_identical": True,
        "integrity_report_bytes_identical": True,
        "capsule_bytes_identical": True,
        "capsules_semantically_identical": True,
        "exclusion_bytes_identical": True,
        "field_visibility_bytes_identical": True,
        "not_performed_reason": None,
    }
    first_final = _finalize_artifacts(first, receipt, Path(repo_root).resolve(strict=True))
    second_final = _finalize_artifacts(second, receipt, Path(repo_root).resolve(strict=True))
    _require(
        set(first_final["file_payloads"]) == set(second_final["file_payloads"])
        and all(
            first_final["file_payloads"][name] == second_final["file_payloads"][name]
            for name in first_final["file_payloads"]
        ),
        "NONDETERMINISTIC_BUILD",
        "independent formal capsule builds are not byte-identical",
        stage="DETERMINISM",
    )
    return first_final


def _manifest_unit_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    reference = record["registry_record_ref"]
    return (
        record["unit_kind"],
        record["unit_id"],
        reference["registry_file_relative_path"],
        reference["record_index"],
    )


def _normalized_exclusion_stage(value: str) -> str:
    mapping = {
        "FROZEN_INPUTS": "REGISTRY",
        "POPULATION": "REGISTRY",
        "REGISTRY": "REGISTRY",
        "MODEL_CONFIG": "REGISTRY",
        "SOURCE": "SOURCE",
        "EVENT_CHAIN": "EVENT_CHAIN",
        "CHAIN": "EVENT_CHAIN",
        "DERIVED_ARTIFACT": "ARTIFACT",
        "ARTIFACT": "ARTIFACT",
        "ARTIFACT_CLOSURE": "ARTIFACT",
        "BLOB": "ARTIFACT",
        "REQUEST": "REQUEST",
        "TASK": "REQUEST",
        "REGIONS": "PARTITION",
        "PARTITION": "PARTITION",
        "STATE": "STATE",
        "TARGET": "TARGET",
        "POST_ACTION": "AUDIT_SUFFIX",
        "AUDIT_SUFFIX": "AUDIT_SUFFIX",
        "BACKEND": "BACKEND",
        "VISIBILITY": "VISIBILITY",
        "READ": "SOURCE",
        "PATH": "SOURCE",
        "FILE_SET": "SOURCE",
        "SCHEMA": "SCHEMA",
        "VALIDATE": "SCHEMA",
        "DETERMINISM": "DETERMINISM",
    }
    return mapping.get(value, "SCHEMA")


def _stable_error_pointer(error: ReplayCapsuleError) -> str | None:
    if error.stage in {
        "ARTIFACT_CLOSURE",
        "BLOB",
        "FILE_SET",
        "PATH",
        "READ",
    }:
        return None
    pointer = error.json_path if isinstance(error.json_path, str) else None
    if pointer == "$" or pointer is None or not pointer.startswith("/"):
        return None
    return pointer


def _exclusion_for(unit: RegistryUnit, error: ReplayCapsuleError) -> dict[str, Any]:
    stage = _normalized_exclusion_stage(error.stage)
    pointer = _stable_error_pointer(error)
    stable_context = []
    for key, value in sorted(error.context.items()):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            continue
        if isinstance(value, (str, int, bool)) or value is None:
            if isinstance(value, str) and (value.startswith("/") or "\\" in value):
                continue
            stable_context.append({"key": key, "value": value})
    expected = error.context.get("expected_sha256")
    observed = error.context.get("observed_sha256")
    expected = expected if isinstance(expected, str) and _SHA_RE.fullmatch(expected) else None
    observed = observed if isinstance(observed, str) and _SHA_RE.fullmatch(observed) else None
    failure = {
        "stage": stage,
        "reason_code": error.code,
        "affected_json_pointer": pointer,
        "expected_sha256": expected,
        "observed_sha256": observed,
        "stable_context": stable_context,
    }
    return {
        "schema_version": EXCLUSION_SCHEMA_VERSION,
        "record_type": "g1_replay_capsule_exclusion",
        "protocol_version": PROTOCOL_VERSION,
        "issue": "ALE-321",
        "story": "G1.3",
        "curated": True,
        "deployment_prediction": False,
        "unit_kind": unit.unit_kind,
        "unit_id": unit.unit_id,
        "original_registry_state": (
            unit.record["case_status"]
            if unit.unit_kind == "STRICT_MHR"
            else unit.record["control_status"]
        ),
        "source_registry_record_ref": _registry_record_ref(unit),
        "capsule_status": "EXCLUDED_FROM_G1_3_CAPSULE_SET",
        "capsule_emitted": False,
        "primary_stage": stage,
        "primary_reason_code": error.code,
        "failures": [failure],
        "failure_ordering": ("STAGE_PRIORITY_THEN_REASON_CODE_THEN_AFFECTED_JSON_POINTER"),
        "retry_behavior": "FAIL_CLOSED_NO_CAPSULE",
        "safety": {
            "provider_invocation_attempted": False,
            "gpu_used": False,
            "gui_action_executed": False,
            "generated_action_executed": False,
            "raw_collector_mutated": False,
            "collector_labels_added": False,
            "g1_1_registry_state_mutated": False,
            "automatic_semantic_inference_performed": False,
            "runtime_sentinel_enabled": False,
        },
    }


def _capsule_set_sha256(units: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(
        [
            {
                "unit_kind": item["unit_kind"],
                "unit_id": item["unit_id"],
                "disposition": item["disposition"],
                "capsule_body_sha256": item["capsule_body_sha256"],
                "exclusion_record_sha256": item["exclusion_record_sha256"],
            }
            for item in units
        ]
    )


def _population_manifest() -> dict[str, Any]:
    return {
        "strict_mhr_candidate_frozen_count": STRICT_TARGET_COUNT,
        "selected_clean_control_count": SELECTED_CLEAN_TARGET_COUNT,
        "target_unit_count": TARGET_POPULATION,
        "reserve_clean_control_out_of_scope_count": RESERVE_CONTROL_COUNT,
        "qwen_target_count": EXPECTED_MODEL_COUNTS["qwen3vl_8b"],
        "mai_target_count": EXPECTED_MODEL_COUNTS["mai_ui_8b"],
        "other_four_history_families_live_capsule_count": 0,
    }


def _population_integrity() -> dict[str, Any]:
    value = _population_manifest()
    value.pop("other_four_history_families_live_capsule_count")
    return {
        "definition": "STRICT_MHR_CANDIDATE_FROZEN_PLUS_SELECTED_CLEAN_CONTROL",
        **value,
    }


def _source_registry_manifest(repo: Path) -> dict[str, Any]:
    return {
        "registry_id": f"sha256:{G1_REGISTRY_MANIFEST_SHA256}",
        "registry_manifest_sha256": G1_REGISTRY_MANIFEST_SHA256,
        "registry_lock_ref": _repo_file_ref(
            repo, "mobileworld_audit_handoff/g1/registry.lock.v1.json"
        ),
        "registry_lock_sha256": G1_REGISTRY_LOCK_SHA256,
        "external_file_set_aggregate_sha256": G1_REGISTRY_AGGREGATE_SHA256,
        "source_config_sha256": G1_SOURCE_CONFIG_SHA256,
        "g1_1_included_count": 0,
        "selection_policy": (
            "STRICT_MHR_CANDIDATE_FROZEN_PLUS_SELECTED_CLEAN_CONTROL_NOT_G1_1_INCLUDED"
        ),
    }


def _builder_contract(
    contract_files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "builder_version": BUILDER_VERSION,
        "capsule_schema_version": CAPSULE_SCHEMA_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "integrity_schema_version": INTEGRITY_SCHEMA_VERSION,
        "contract_amendment_version": CONTRACT_AMENDMENT_VERSION,
        "canonicalization": "mobileworld.canonical-json/sorted-keys-utf8-no-nan/v1",
        "capsule_body_hash_subject": "CANONICAL_INNER_CAPSULE_OBJECT",
        "unit_order": "UNIT_KIND_THEN_UNIT_ID_THEN_REGISTRY_FILE_THEN_RECORD_INDEX",
        "artifact_order": "STORE_ID_THEN_RELATIVE_PATH_THEN_SHA256",
        "failure_order": "STAGE_PRIORITY_THEN_REASON_CODE_THEN_AFFECTED_JSON_POINTER",
        "contract_files": [
            {
                "relative_path": record["path"],
                "sha256": record["sha256"],
                "byte_count": record["byte_count"],
            }
            for record in contract_files
        ],
        "build_timestamp_in_identity": False,
        "absolute_output_path_in_identity": False,
    }


def _candidate_double_build_receipt() -> dict[str, Any]:
    return {
        "status": "NOT_PERFORMED",
        "performed": False,
        "comparison_scope": "CORE_FILES_EXCLUDING_INTEGRITY_AND_MANIFEST",
        "first_core_file_set_sha256": None,
        "second_core_file_set_sha256": None,
        "output_file_set_bytes_identical": None,
        "manifest_bytes_identical": None,
        "integrity_report_bytes_identical": None,
        "capsule_bytes_identical": None,
        "capsules_semantically_identical": None,
        "exclusion_bytes_identical": None,
        "field_visibility_bytes_identical": None,
        "not_performed_reason": "SINGLE_BUILD_CANDIDATE",
    }


def _publication_policy() -> dict[str, Any]:
    return {
        "status": "PREINSTALL_NOT_PUBLISHED",
        "policy": {
            "atomic_no_replace_required": True,
            "write_once_required": True,
            "regular_files_only_required": True,
            "zero_symlinks_required": True,
            "exact_manifest_file_set_required": True,
            "read_only_install_required": True,
        },
        "final_root_observation": (
            "EXTERNAL_READ_ONLY_DIRECTORY_VALIDATOR_OWNS_POST_INSTALL_FACTS"
        ),
        "post_install_verification_required": True,
    }


def _integrity_check(
    check_code: str,
    *,
    status: str = "PASS",
    affected_json_pointer: str | None = None,
    expected_sha256: str | None = None,
    observed_sha256: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "check_code": check_code,
        "status": status,
        "affected_json_pointer": affected_json_pointer,
        "expected_sha256": expected_sha256,
        "observed_sha256": observed_sha256,
        "reason_code": reason_code,
    }


def _unit_checks(
    record: Mapping[str, Any],
    exclusion_by_unit: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if record["disposition"] == "EXCLUDED":
        exclusion = (exclusion_by_unit or {}).get(record["unit_id"])
        reason_code = (
            exclusion["primary_reason_code"]
            if exclusion is not None
            else "SCHEMA_VALIDATION_FAILED"
        )
        affected_pointer = (
            exclusion["failures"][0]["affected_json_pointer"] if exclusion is not None else None
        )
        check_by_reason = {
            "REGISTRY_BINDING_INVALID": "REGISTRY_RECORD_BINDING",
            "SOURCE_REFERENCE_UNRESOLVED": "SOURCE_MANIFEST_BINDING",
            "SOURCE_HASH_MISMATCH": "TASK_STREAM_HASH",
            "RAW_EVENT_CHAIN_INVALID": "EVENT_CHAIN",
            "BLOB_REFERENCE_INVALID": "TRANSITIVE_BLOB_CLOSURE",
            "BLOB_MISSING": "TRANSITIVE_BLOB_CLOSURE",
            "BLOB_HASH_MISMATCH": "TRANSITIVE_BLOB_CLOSURE",
            "ARTIFACT_REHYDRATION_FAILED": "ARTIFACT_GRAPH_REHYDRATION",
            "REQUEST_HASH_MISMATCH": "SEMANTIC_REQUEST_HASH",
            "REQUEST_VIEW_MISMATCH": "REQUEST_VIEW_CONSISTENCY",
            "REQUEST_PARTITION_AMBIGUOUS": "REQUEST_REGION_PARTITION",
            "REQUEST_PARTITION_INCOMPLETE": "REQUEST_REGION_PARTITION",
            "NON_HISTORY_REGION_UNRECOVERABLE": "NON_HISTORY_RECOVERABILITY",
            "STATE_HASH_MISMATCH": "CURRENT_STATE_HASH",
            "CURRENT_OBSERVATION_UNRESOLVED": "CURRENT_STATE_HASH",
            "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED": ("CURRENT_SCREENSHOT_COORDINATE"),
            "TARGET_SPAN_UNRESOLVED": "TARGET_UNIQUE_RESOLUTION",
            "TARGET_SPAN_AMBIGUOUS": "TARGET_UNIQUE_RESOLUTION",
            "TARGET_SPAN_HASH_MISMATCH": "TARGET_DUAL_COORDINATES",
            "TARGET_SPAN_COORDINATE_MISMATCH": "TARGET_DUAL_COORDINATES",
            "TARGET_SET_OVERLAP": "TARGET_SET_NON_OVERLAP",
            "ORIGINAL_RESPONSE_UNRESOLVED": "AUDIT_SUFFIX_LINKAGE",
            "ORIGINAL_ACTION_UNRESOLVED": "AUDIT_SUFFIX_LINKAGE",
            "ORIGINAL_TRANSITION_UNRESOLVED": "AUDIT_SUFFIX_LINKAGE",
            "BACKEND_DEPENDENCY_UNPROVEN": "RESTORE_DESCRIPTOR",
            "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING": "RESTORE_DESCRIPTOR",
            "PREFIX_REPLAY_RECIPE_INVALID": "RESTORE_DESCRIPTOR",
            "FUTURE_EVIDENCE_LEAKAGE": "NO_FUTURE_RUNTIME_LEAKAGE",
            "FIELD_VISIBILITY_INVALID": "NO_FUTURE_RUNTIME_LEAKAGE",
            "CURATOR_CHANNEL_VIOLATION": "CURATOR_CHANNEL_ISOLATION",
            "CAPSULE_HASH_MISMATCH": "CAPSULE_BODY_HASH",
        }
        return [
            _integrity_check(
                check_by_reason.get(reason_code, "CAPSULE_SCHEMA"),
                status="FAIL",
                affected_json_pointer=affected_pointer,
                reason_code=reason_code,
            )
        ]
    return [
        _integrity_check(code)
        for code in (
            "REGISTRY_RECORD_BINDING",
            "SOURCE_MANIFEST_BINDING",
            "TASK_STREAM_HASH",
            "EVENT_CHAIN",
            "TRANSITIVE_BLOB_CLOSURE",
            "ARTIFACT_GRAPH_REHYDRATION",
            "SEMANTIC_REQUEST_HASH",
            "REQUEST_VIEW_CONSISTENCY",
            "REQUEST_REGION_PARTITION",
            "NON_HISTORY_RECOVERABILITY",
            "CURRENT_STATE_HASH",
            "CURRENT_SCREENSHOT_COORDINATE",
            "TARGET_UNIQUE_RESOLUTION",
            "TARGET_DUAL_COORDINATES",
            "TARGET_SET_NON_OVERLAP",
            "AUDIT_SUFFIX_LINKAGE",
            "NO_FUTURE_RUNTIME_LEAKAGE",
            "CURATOR_CHANNEL_ISOLATION",
            "RESTORE_DESCRIPTOR",
            "CAPSULE_SCHEMA",
            "CAPSULE_BODY_HASH",
        )
    ]


def _build_integrity_report(
    *,
    phase: str,
    capsules: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
    capsule_set_sha256: str,
    capsule_index: Sequence[Mapping[str, Any]],
    exclusion_ledger_ref: Mapping[str, Any],
    source_snapshot_before: str,
    source_snapshot_after: str,
    double_build: Mapping[str, Any],
    counts: Mapping[str, Any],
) -> dict[str, Any]:
    del capsules
    exclusion_by_unit = {record["unit_id"]: record for record in exclusions}
    receipts: list[dict[str, Any]] = []
    for record in capsule_index:
        receipts.append(
            {
                "unit_kind": record["unit_kind"],
                "unit_id": record["unit_id"],
                "source_key": record["source_key"],
                "history_family": record["history_family"],
                "disposition": record["disposition"],
                "capsule_ref": _json_clone(record["capsule_ref"]),
                "capsule_body_sha256": record["capsule_body_sha256"],
                "exclusion_ref": (
                    _json_clone(exclusion_ledger_ref)
                    if record["disposition"] == "EXCLUDED"
                    else None
                ),
                "exclusion_sha256": record["exclusion_record_sha256"],
                "checks": _unit_checks(record, exclusion_by_unit),
                "valid_capsule": record["disposition"] == "CAPSULED",
            }
        )
    integrity_counts = {
        "target_unit_count": TARGET_POPULATION,
        "capsuled_count": counts["capsuled_count"],
        "excluded_count": counts["excluded_count"],
        "unit_receipt_count": len(receipts),
        "capsuled_unit_failed_check_count": 0,
        "unaccounted_unit_count": counts["unaccounted_unit_count"],
        "duplicate_unit_count": counts["duplicate_unit_count"],
    }
    safety = _safety_flags()
    safety.pop("collector_labels_added")
    return {
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "record_type": "g1_replay_capsule_integrity_report",
        "protocol_version": PROTOCOL_VERSION,
        "issue": "ALE-321",
        "story": "G1.3",
        "report_phase": phase,
        "curated": True,
        "deployment_prediction": False,
        "source_registry_manifest_sha256": G1_REGISTRY_MANIFEST_SHA256,
        "capsule_set_sha256": capsule_set_sha256,
        "capsule_set_hash_subject": (
            "CANONICAL_ORDERED_190_UNIT_DISPOSITIONS_WITH_CAPSULE_OR_EXCLUSION_HASH"
        ),
        "population": _population_integrity(),
        "counts": integrity_counts,
        "unit_receipts": receipts,
        "source_immutability": {
            "verified": True,
            "pre_build_source_file_set_sha256": source_snapshot_before,
            "post_build_source_file_set_sha256": source_snapshot_after,
            "raw_events_or_blobs_mutated": False,
            "collector_labels_added": False,
        },
        "double_build": _json_clone(double_build),
        "publication": _publication_policy(),
        "report_valid": True,
        "safety": safety,
    }


def _build_manifest(
    *,
    repo: Path,
    phase: str,
    contract_files: Sequence[Mapping[str, Any]],
    capsule_set_sha256: str,
    capsule_index: Sequence[Mapping[str, Any]],
    capsule_file_refs: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    capsule_refs = sorted(
        (_json_clone(record) for record in capsule_file_refs),
        key=lambda record: record["relative_path"],
    )
    artifact_refs = [
        _file_summary(data, name)
        for name, data in sorted(payloads.items())
        if name.startswith("artifact-")
    ]
    payload_aggregate = _file_set_aggregate(payloads)
    formal = phase == "FORMAL_PUBLICATION_READY"
    all_capsuled = counts["capsuled_count"] == TARGET_POPULATION
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "record_type": "g1_replay_capsule_publication_manifest",
        "protocol_version": PROTOCOL_VERSION,
        "portable_contract_version": PORTABLE_CONTRACT_VERSION,
        "issue": "ALE-321",
        "story": "G1.3",
        "publication_phase": phase,
        "curated": True,
        "deployment_prediction": False,
        "source_registry": _source_registry_manifest(repo),
        "population": _population_manifest(),
        "builder_contract": _builder_contract(contract_files),
        "capsule_set_sha256": capsule_set_sha256,
        "capsule_set_hash_subject": (
            "CANONICAL_ORDERED_190_UNIT_DISPOSITIONS_WITH_CAPSULE_OR_EXCLUSION_HASH"
        ),
        "units": [_json_clone(item) for item in capsule_index],
        "files": {
            "field_visibility_policy": _file_summary(
                payloads["field_visibility.json"], "field_visibility.json"
            ),
            "capsule_index": _file_summary(payloads["capsule_index.jsonl"], "capsule_index.jsonl"),
            "capsule_files": capsule_refs,
            "artifact_files": artifact_refs,
            "exclusion_ledger": _file_summary(
                payloads["capsule_exclusions.jsonl"], "capsule_exclusions.jsonl"
            ),
            "integrity_report": _file_summary(
                payloads["capsule_integrity.json"], "capsule_integrity.json"
            ),
            "payload_file_count_excluding_manifest": len(payloads),
            "final_root_file_count": len(payloads) + 1,
            "payload_file_set_aggregate_sha256": payload_aggregate,
            "payload_file_set_aggregate_subject": (
                "CANONICAL_FILENAME_TO_SHA256_MAP_EXCLUDING_MANIFEST"
            ),
        },
        "counts": _json_clone(counts),
        "finalization": {
            "integrity_report_phase": phase,
            "double_build_status": "PASSED" if formal else "NOT_PERFORMED",
            "manifest_hash_subject": "EXACT_CANONICAL_MANIFEST_FILE_BYTES",
            "publication_install_status": "PREINSTALL_NOT_PUBLISHED",
            "formal_publication_allowed": formal,
            "post_install_facts_location": ("EXTERNAL_READ_ONLY_DIRECTORY_VALIDATION_RECEIPT"),
        },
        "readiness": {
            "capsule_materialization_complete": True,
            "capsule_validation_complete": True,
            "all_target_units_capsuled": all_capsuled,
            "formal_acceptance_ready": formal and all_capsuled,
            "execution_ready": False,
            "provider_invocation_allowed": False,
            "run_ready": False,
            "provider_codec_ready": False,
            "live_history_codec_ready": False,
            "gold_and_transformations_ready": False,
            "treatment_response_generation_allowed": False,
        },
        "safety": _manifest_safety_flags(),
    }


def _core_file_payloads(payloads: Mapping[str, bytes]) -> dict[str, bytes]:
    return {
        name: data
        for name, data in payloads.items()
        if name not in {"capsule_integrity.json", "capsule_manifest.json"}
    }


def _file_set_aggregate(payloads: Mapping[str, bytes]) -> str:
    return canonical_sha256({name: sha256_bytes(data) for name, data in sorted(payloads.items())})


def _finalize_artifacts(
    candidate: Mapping[str, Any], double_build: Mapping[str, Any], repo: Path
) -> dict[str, Any]:
    payloads = dict(candidate["file_payloads"])
    payloads.pop("capsule_manifest.json", None)
    exclusion_ref = _file_summary(payloads["capsule_exclusions.jsonl"], "capsule_exclusions.jsonl")
    source_immutability = candidate["integrity"]["source_immutability"]
    integrity = _build_integrity_report(
        phase="FORMAL_PUBLICATION_READY",
        capsules=candidate["capsules"],
        exclusions=candidate["exclusions"],
        capsule_set_sha256=candidate["manifest"]["capsule_set_sha256"],
        capsule_index=candidate["manifest"]["units"],
        exclusion_ledger_ref=exclusion_ref,
        source_snapshot_before=source_immutability["pre_build_source_file_set_sha256"],
        source_snapshot_after=source_immutability["post_build_source_file_set_sha256"],
        double_build=double_build,
        counts=candidate["manifest"]["counts"],
    )
    payloads["capsule_integrity.json"] = canonical_json_line(integrity)
    contract_files = [
        {
            "path": record["relative_path"],
            "sha256": record["sha256"],
            "byte_count": record["byte_count"],
        }
        for record in candidate["manifest"]["builder_contract"]["contract_files"]
    ]
    manifest = _build_manifest(
        repo=repo,
        phase="FORMAL_PUBLICATION_READY",
        contract_files=contract_files,
        capsule_set_sha256=candidate["manifest"]["capsule_set_sha256"],
        capsule_index=candidate["manifest"]["units"],
        capsule_file_refs=candidate["manifest"]["files"]["capsule_files"],
        counts=candidate["manifest"]["counts"],
        payloads=payloads,
    )
    payloads["capsule_manifest.json"] = canonical_json_line(manifest)
    final = {
        "capsules": candidate["capsules"],
        "exclusions": candidate["exclusions"],
        "visibility": candidate["visibility"],
        "integrity": integrity,
        "manifest": manifest,
        "file_payloads": payloads,
    }
    _validate_artifacts_against_schemas(repo, final)
    return final


def field_visibility_policy() -> dict[str, Any]:
    """Return the frozen deny-by-default five-class visibility policy."""

    return {
        "schema_version": "mobileworld.g1.replay-capsule.field-visibility/v1",
        "default_policy": "DENY",
        "classification_complete": True,
        "overlap_policy": "ROOTS_NON_OVERLAPPING",
        "rules": [
            {
                "classification": "FROZEN_MODEL_VISIBLE",
                "root_json_pointer": "/runtime/model_visible",
                "allowed_consumers": ["HISTORY_CODEC", "PROTOCOL_VALIDATOR"],
                "mutable": False,
                "may_reference_post_cutoff": False,
                "direct_provider_input": False,
            },
            {
                "classification": "FROZEN_NON_HISTORY_ENVELOPE",
                "root_json_pointer": "/runtime/non_history_envelope",
                "allowed_consumers": ["REPLAY_HARNESS", "PROTOCOL_VALIDATOR"],
                "mutable": False,
                "may_reference_post_cutoff": False,
                "direct_provider_input": False,
            },
            {
                "classification": "MUTABLE_HISTORY_TREATMENT",
                "root_json_pointer": "/runtime/treatment_surface",
                "allowed_consumers": [
                    "HISTORY_CODEC",
                    "PROTOCOL_VALIDATOR",
                    "TRANSFORMATION_CURATOR",
                ],
                "mutable": True,
                "may_reference_post_cutoff": False,
                "direct_provider_input": False,
            },
            {
                "classification": "CURATOR_ONLY",
                "root_json_pointer": "/curator_only",
                "allowed_consumers": [
                    "ACTION_GOLD_CURATOR",
                    "TRANSFORMATION_CURATOR",
                    "PROTOCOL_VALIDATOR",
                ],
                "mutable": False,
                "may_reference_post_cutoff": False,
                "direct_provider_input": False,
            },
            {
                "classification": "POST_ACTION_AUDIT_ONLY",
                "root_json_pointer": "/post_action_audit",
                "allowed_consumers": ["AUDIT_VALIDATOR"],
                "mutable": False,
                "may_reference_post_cutoff": True,
                "direct_provider_input": False,
            },
        ],
        "curator_channels": [
            {
                "channel": "ACTION_GOLD",
                "root_json_pointer": "/curator_only/action_gold",
                "allowed_consumer": "ACTION_GOLD_CURATOR",
                "history_visible": False,
                "natural_target_output_visible": False,
                "later_trajectory_visible": False,
                "event_cutoff_policy": "AT_OR_BEFORE_REQUEST_CUTOFF",
            },
            {
                "channel": "TRANSFORMATION",
                "root_json_pointer": "/curator_only/transformation",
                "allowed_consumer": "TRANSFORMATION_CURATOR",
                "history_visible": True,
                "natural_target_output_visible": False,
                "later_trajectory_visible": False,
                "event_cutoff_policy": "AT_OR_BEFORE_REQUEST_CUTOFF",
            },
        ],
        "renderer_input_roots": ["/runtime/model_visible", "/runtime/treatment_surface"],
        "harness_input_roots": ["/runtime/non_history_envelope"],
        "forbidden_runtime_roots": [
            "/unit",
            "/source_provenance",
            "/curator_only",
            "/post_action_audit",
            "/field_visibility",
            "/artifact_closure",
            "/integrity_binding",
            "/safety",
        ],
        "validator_metadata_roots": [
            "/capsule_id",
            "/protocol_version",
            "/portable_contract_version",
            "/issue",
            "/story",
            "/curated",
            "/deployment_prediction",
            "/unit",
            "/source_provenance",
            "/field_visibility",
            "/artifact_closure",
            "/integrity_binding",
            "/safety",
        ],
        "validator_metadata_consumers": ["PROTOCOL_VALIDATOR", "AUDIT_VALIDATOR"],
        "validator_metadata_direct_provider_input": False,
    }


def validate_capsule_directory(
    capsule_root: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
    registry_root: str | os.PathLike[str] | None = None,
    source_base: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate a publication structurally and, when requested, against sources."""

    supplied = Path(capsule_root)
    _require(not supplied.is_symlink(), "SOURCE_REFERENCE_UNRESOLVED", "root is symlink")
    root = supplied.resolve(strict=True)
    root_metadata = root.lstat()
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    validation_repo = (
        Path(repo_root).resolve(strict=True)
        if repo_root is not None
        else Path(__file__).resolve().parents[4]
    )
    payloads = _validate_publication_file_set(root, validation_repo)
    manifest_bytes = payloads["capsule_manifest.json"]
    manifest = _load_canonical_object_bytes(manifest_bytes, root / "capsule_manifest.json")
    _require(
        sha256_bytes(manifest_bytes) == root.name,
        "CAPSULE_HASH_MISMATCH",
        "content-address directory does not match manifest bytes",
    )
    _require(
        manifest.get("publication_phase") == "FORMAL_PUBLICATION_READY"
        and manifest.get("finalization", {}).get("formal_publication_allowed") is True
        and manifest.get("readiness", {}).get("formal_acceptance_ready") is True,
        "SCHEMA_VALIDATION_FAILED",
        "published directory is not a formal G1.3 artifact set",
        stage="SCHEMA",
    )
    integrity = _load_canonical_object_bytes(
        payloads["capsule_integrity.json"], root / "capsule_integrity.json"
    )
    visibility = _load_canonical_object_bytes(
        payloads["field_visibility.json"], root / "field_visibility.json"
    )
    index = [
        _load_canonical_line_bytes(line, root / "capsule_index.jsonl", line_number)
        for line_number, line in enumerate(
            payloads["capsule_index.jsonl"].splitlines(keepends=True), start=1
        )
    ]
    _require(
        index == manifest["units"],
        "CAPSULE_HASH_MISMATCH",
        "capsule index differs from the schema-valid manifest unit table",
        stage="SCHEMA",
    )
    capsules = [
        _load_canonical_object_bytes(
            payloads[record["capsule_ref"]["relative_path"]],
            root / record["capsule_ref"]["relative_path"],
        )
        for record in index
        if record["disposition"] == "CAPSULED"
    ]
    exclusions = [
        _load_canonical_line_bytes(line, root / "capsule_exclusions.jsonl", line_number)
        for line_number, line in enumerate(
            payloads["capsule_exclusions.jsonl"].splitlines(keepends=True), start=1
        )
    ]
    _require(
        len(capsules) + len(exclusions) == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "published dispositions do not cover target population",
    )
    artifacts = {
        "capsules": capsules,
        "exclusions": exclusions,
        "visibility": visibility,
        "integrity": integrity,
        "manifest": manifest,
        "file_payloads": payloads,
    }
    _validate_artifacts_against_schemas(validation_repo, artifacts)
    artifact_schema_generation = _artifact_schema_generation(artifacts)
    source_rebuild_requested = any(
        value is not None for value in (repo_root, registry_root, source_base)
    )
    source_rebuild_performed = False
    if source_rebuild_requested:
        _require(
            all(value is not None for value in (repo_root, registry_root, source_base)),
            "SOURCE_REFERENCE_UNRESOLVED",
            "source rebuild arguments must be complete",
        )
        assert repo_root is not None
        assert registry_root is not None
        assert source_base is not None
        rebuilt = build_verified_capsule_artifacts(
            repo_root=repo_root, registry_root=registry_root, source_base=source_base
        )
        _require(
            set(payloads) == set(rebuilt["file_payloads"])
            and all(payloads[name] == rebuilt["file_payloads"][name] for name in payloads),
            "NONDETERMINISTIC_BUILD",
            "published file set differs from the source-bound formal rebuild",
            stage="DETERMINISM",
        )
        source_rebuild_performed = True
    final_payloads = _validate_publication_file_set(root, validation_repo)
    final_metadata = root.lstat()
    _require(
        (final_metadata.st_dev, final_metadata.st_ino) == root_identity
        and set(final_payloads) == set(payloads)
        and all(final_payloads[name] == payloads[name] for name in payloads),
        "SOURCE_REFERENCE_UNRESOLVED",
        "publication root changed during validation",
        stage="SCHEMA",
    )
    return {
        "valid": True,
        "schema_version": "mobileworld.g1.replay-capsule-directory-validation/v1.1",
        "artifact_schema_generation": artifact_schema_generation,
        "capsule_schema_version": (
            CAPSULE_SCHEMA_VERSION
            if artifact_schema_generation == "ACTIVE_V1_1"
            else LEGACY_CAPSULE_SCHEMA_VERSION
        ),
        "superseded_for_formal_g1": artifact_schema_generation == "LEGACY_V1",
        "validation_scope": ("SOURCE_BOUND" if source_rebuild_performed else "STRUCTURAL_ONLY"),
        "structural_valid": True,
        "source_bound_valid": source_rebuild_performed,
        "formal_publication_valid": source_rebuild_performed,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "capsule_set_sha256": manifest["capsule_set_sha256"],
        "file_count": len(payloads),
        "total_byte_count": sum(len(data) for data in payloads.values()),
        "exact_file_set": True,
        "regular_files_only": True,
        "zero_symlinks": True,
        "read_only": True,
        "source_rebuild_performed": source_rebuild_performed,
        "source_rebuild_byte_identical": source_rebuild_performed,
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "raw_collector_mutated": False,
    }


def write_capsule_artifacts(
    artifacts: Mapping[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
    registry_root: str | os.PathLike[str] | None = None,
    source_base: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Atomically install one formal immutable content-addressed publication."""

    _require(
        all(value is not None for value in (repo_root, registry_root, source_base)),
        "SOURCE_REFERENCE_UNRESOLVED",
        "formal publication requires complete source-bound validation roots",
        stage="SOURCE",
    )
    assert repo_root is not None
    assert registry_root is not None
    assert source_base is not None
    destination = Path(output_dir)
    _require(not destination.exists(), "SOURCE_REFERENCE_UNRESOLVED", "output exists")
    parent = destination.parent.resolve(strict=True)
    parent_metadata = parent.stat()
    parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
    resolved = parent / destination.name
    repository = Path(repo_root).resolve(strict=True)
    resolved_registry = Path(registry_root).resolve(strict=True)
    resolved_source = Path(source_base).resolve(strict=True)
    protected_roots = [repository]
    protected_roots.extend((resolved_registry, resolved_source))
    _require(
        all(not _is_within(resolved, protected) for protected in protected_roots),
        "SOURCE_REFERENCE_UNRESOLVED",
        "capsule publication is forbidden inside repository or source roots",
    )
    _require(
        artifacts.get("manifest", {}).get("publication_phase") == "FORMAL_PUBLICATION_READY"
        and artifacts.get("manifest", {}).get("finalization", {}).get("formal_publication_allowed")
        is True,
        "SCHEMA_VALIDATION_FAILED",
        "only a double-build-verified formal artifact set may be published",
        stage="SCHEMA",
    )
    _require_active_artifact_generation(artifacts)
    payloads = artifacts.get("file_payloads")
    _require(
        isinstance(payloads, Mapping)
        and BASE_OUTPUT_FILE_NAMES.issubset(payloads)
        and all(_valid_output_name(name) for name in payloads),
        "SCHEMA_VALIDATION_FAILED",
        "artifact file set invalid",
    )
    assert isinstance(payloads, Mapping)
    _validate_artifacts_against_schemas(repository, artifacts)
    manifest_bytes = payloads["capsule_manifest.json"]
    manifest_sha256 = sha256_bytes(manifest_bytes)
    _require(
        destination.name == manifest_sha256,
        "CAPSULE_HASH_MISMATCH",
        "publication directory name is not the exact manifest SHA-256",
        stage="SCHEMA",
    )
    source_rebuilt = build_verified_capsule_artifacts(
        repo_root=repository,
        registry_root=resolved_registry,
        source_base=resolved_source,
    )
    _require(
        set(payloads) == set(source_rebuilt["file_payloads"])
        and all(payloads[name] == source_rebuilt["file_payloads"][name] for name in payloads),
        "NONDETERMINISTIC_BUILD",
        "publication input differs from an independent source-bound formal rebuild",
        stage="DETERMINISM",
    )
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=parent))
    temporary_metadata = temporary.lstat()
    temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
    installed = False
    parent_fd: int | None = None
    try:
        for name, data in sorted(payloads.items()):
            _require(isinstance(data, bytes), "SCHEMA_VALIDATION_FAILED", "payload not bytes")
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o444, follow_symlinks=False)
        temporary_fd = os.open(
            temporary, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.fchmod(temporary_fd, 0o555)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        _rename_directory_noreplace(
            parent_fd=parent_fd,
            source_name=temporary.name,
            destination_name=destination.name,
        )
        installed = True
        os.fsync(parent_fd)
        after_install_parent = os.fstat(parent_fd)
        _require(
            (after_install_parent.st_dev, after_install_parent.st_ino) == parent_identity,
            "SOURCE_REFERENCE_UNRESOLVED",
            "publication parent changed during atomic installation",
            stage="SCHEMA",
        )
    except Exception:
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        if not installed:
            try:
                current_metadata: os.stat_result | None = temporary.lstat()
            except OSError:
                current_metadata = None
            if (
                current_metadata is not None
                and stat.S_ISDIR(current_metadata.st_mode)
                and not stat.S_ISLNK(current_metadata.st_mode)
                and (current_metadata.st_dev, current_metadata.st_ino) == temporary_identity
            ):
                try:
                    os.chmod(temporary, 0o700, follow_symlinks=False)
                except OSError:
                    pass
                shutil.rmtree(temporary, ignore_errors=True)
    final_parent = parent.stat()
    _require(
        (final_parent.st_dev, final_parent.st_ino) == parent_identity,
        "SOURCE_REFERENCE_UNRESOLVED",
        "publication parent changed before post-install validation",
        stage="SCHEMA",
    )
    return validate_capsule_directory(
        destination,
        repo_root=repository,
        registry_root=resolved_registry,
        source_base=resolved_source,
    )


def _rename_directory_noreplace(*, parent_fd: int, source_name: str, destination_name: str) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE); fail closed if unavailable."""

    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    _require(
        function is not None,
        "SOURCE_REFERENCE_UNRESOLVED",
        "atomic no-replace directory installation is unavailable",
        stage="SCHEMA",
    )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ReplayCapsuleError(
            "SOURCE_REFERENCE_UNRESOLVED",
            "write-once publication target already exists",
            stage="SCHEMA",
        )
    raise ReplayCapsuleError(
        "SOURCE_REFERENCE_UNRESOLVED",
        "atomic no-replace publication failed",
        stage="SCHEMA",
        errno=error_number,
    )


def _load_frozen_inputs(repo: Path, registry: Path) -> dict[str, Any]:
    lock_path = repo / "mobileworld_audit_handoff/g1/registry.lock.v1.json"
    lock_bytes = _read_regular(lock_path)
    _require(
        sha256_bytes(lock_bytes) == G1_REGISTRY_LOCK_SHA256,
        "REGISTRY_BINDING_INVALID",
        "G1.1 registry lock bytes changed",
        stage="FROZEN_INPUTS",
        json_path="/registry_lock",
    )
    lock = _load_pinned_object_bytes(lock_bytes, lock_path)
    external = lock.get("external_registry")
    _require(
        isinstance(external, Mapping),
        "REGISTRY_BINDING_INVALID",
        "G1.1 external registry lock missing",
        stage="FROZEN_INPUTS",
    )
    _require(
        registry.name == G1_REGISTRY_MANIFEST_SHA256,
        "REGISTRY_BINDING_INVALID",
        "registry directory is not the frozen content address",
        stage="FROZEN_INPUTS",
    )
    expected_files = external.get("files")
    _require(
        isinstance(expected_files, Mapping),
        "REGISTRY_BINDING_INVALID",
        "registry file lock missing",
        stage="FROZEN_INPUTS",
    )
    _validate_exact_regular_file_set(registry, set(expected_files))
    for name, summary in expected_files.items():
        _verify_file_summary(_read_regular(registry / name), summary, name)
    manifest_bytes = _read_regular(registry / "registry_manifest.json")
    _require(
        sha256_bytes(manifest_bytes) == G1_REGISTRY_MANIFEST_SHA256,
        "REGISTRY_BINDING_INVALID",
        "G1.1 registry manifest changed",
        stage="FROZEN_INPUTS",
    )
    aggregate = sha256_bytes(
        canonical_json_line(
            {name: sha256_bytes(_read_regular(registry / name)) for name in sorted(expected_files)}
        )
    )
    _require(
        aggregate == G1_REGISTRY_AGGREGATE_SHA256,
        "REGISTRY_BINDING_INVALID",
        "G1.1 registry aggregate changed",
        stage="FROZEN_INPUTS",
    )

    source_config_path = repo / "mobileworld_audit_handoff/g1/source_registry_inputs.v1.json"
    source_config_bytes = _read_regular(source_config_path)
    _require(
        sha256_bytes(source_config_bytes) == G1_SOURCE_CONFIG_SHA256,
        "REGISTRY_BINDING_INVALID",
        "G1.1 source config changed",
        stage="FROZEN_INPUTS",
    )
    source_config = _load_pinned_object_bytes(source_config_bytes, source_config_path)
    contract_files = source_config.get("contract_files")
    _require(
        isinstance(contract_files, list) and len(contract_files) == 25,
        "REGISTRY_BINDING_INVALID",
        "G1.1 contract file list changed",
        stage="FROZEN_INPUTS",
    )
    actual_contract: dict[str, str] = {}
    for record in contract_files:
        _require(
            isinstance(record, Mapping)
            and isinstance(record.get("path"), str)
            and isinstance(record.get("sha256"), str),
            "REGISTRY_BINDING_INVALID",
            "G1.1 contract entry invalid",
            stage="FROZEN_INPUTS",
        )
        relative = PurePosixPath(record["path"])
        _require(
            not relative.is_absolute() and ".." not in relative.parts,
            "REGISTRY_BINDING_INVALID",
            "G1.1 contract path invalid",
            stage="FROZEN_INPUTS",
        )
        data = _read_regular(repo.joinpath(*relative.parts))
        digest = sha256_bytes(data)
        _require(
            digest == record["sha256"],
            "REGISTRY_BINDING_INVALID",
            "G1.1 contract bytes changed",
            stage="FROZEN_INPUTS",
            json_path=f"/contract_files/{record['path']}",
        )
        actual_contract[record["path"]] = digest
    contract_aggregate = canonical_sha256(
        {
            "source_config": {
                "path": "mobileworld_audit_handoff/g1/source_registry_inputs.v1.json",
                "sha256": G1_SOURCE_CONFIG_SHA256,
            },
            "contract_files": [
                {"path": path, "sha256": actual_contract[path]} for path in sorted(actual_contract)
            ],
        }
    )
    # The G1.1 builder uses this exact aggregate construction.  Keep the
    # frozen constant as the external authority even if this implementation's
    # local explanatory reconstruction changes in a future version.
    _require(
        lock["registry_contract"]["aggregate_sha256"] == G1_CONTRACT_AGGREGATE_SHA256,
        "REGISTRY_BINDING_INVALID",
        "G1.1 contract aggregate lock changed",
        stage="FROZEN_INPUTS",
    )
    del contract_aggregate

    model_path = repo / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
    model_bytes = _read_regular(model_path)
    _require(
        sha256_bytes(model_bytes) == MODEL_MANIFEST_SHA256,
        "REGISTRY_BINDING_INVALID",
        "model configuration manifest changed",
        stage="FROZEN_INPUTS",
    )
    model_manifest = _load_pinned_object_bytes(model_bytes, model_path)
    runtime_boundary = model_manifest.get("runtime_boundary")
    _require(
        isinstance(runtime_boundary, Mapping)
        and runtime_boundary.get("backend_dependency") == "NONE_FOR_SERIALIZED_MODEL_CALL"
        and runtime_boundary.get("backend_checkpoint_required") is False
        and runtime_boundary.get("backend_checkpoint_reference") is None
        and runtime_boundary.get("generated_action_execution_allowed") is False
        and runtime_boundary.get("collector_mutation_allowed") is False,
        "BACKEND_DEPENDENCY_UNPROVEN",
        "model manifest does not prove the serialized-request-only boundary",
        stage="BACKEND",
    )

    strict_units = _registry_units(
        registry / "case_registry.pre_gold.jsonl",
        registry_file="case_registry.pre_gold.jsonl",
        unit_kind="STRICT_MHR",
        id_key="case_id",
        predicate=lambda value: value.get("case_kind") == "STRICT_MHR"
        and value.get("case_status") == "CANDIDATE_FROZEN",
    )
    clean_selected = _registry_units(
        registry / "clean_control_pool.jsonl",
        registry_file="clean_control_pool.jsonl",
        unit_kind="CLEAN_CONTROL",
        id_key="control_id",
        predicate=lambda value: value.get("control_status") == "SELECTED",
    )
    clean_all = _load_canonical_jsonl(registry / "clean_control_pool.jsonl")
    reserve_count = sum(record.get("control_status") == "RESERVE" for record in clean_all)
    _require(
        len(strict_units) == STRICT_TARGET_COUNT
        and len(clean_selected) == SELECTED_CLEAN_TARGET_COUNT
        and reserve_count == RESERVE_CONTROL_COUNT,
        "REGISTRY_BINDING_INVALID",
        "G1.1 target population changed",
        stage="POPULATION",
    )
    population = sorted((*strict_units, *clean_selected), key=lambda value: value.unit_id)
    _require(
        len(population) == TARGET_POPULATION
        and len({unit.unit_id for unit in population}) == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "G1.3 target population is not one-to-one",
        stage="POPULATION",
    )
    model_counts = Counter(unit.record["model_id"] for unit in population)
    _require(
        dict(model_counts) == EXPECTED_MODEL_COUNTS,
        "REGISTRY_BINDING_INVALID",
        "G1.3 target model census changed",
        stage="POPULATION",
    )
    return {
        "lock": lock,
        "source_config": source_config,
        "model_manifest": model_manifest,
        "population": population,
    }


def _registry_units(
    path: Path,
    *,
    registry_file: str,
    unit_kind: str,
    id_key: str,
    predicate: Any,
) -> list[RegistryUnit]:
    data = _read_regular(path)
    file_sha256 = sha256_bytes(data)
    units: list[RegistryUnit] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        _require(
            line.endswith(b"\n"),
            "REGISTRY_BINDING_INVALID",
            "registry JSONL line is not newline terminated",
            stage="POPULATION",
            json_path=f"/{registry_file}/{line_number}",
        )
        record = _load_canonical_line_bytes(line, path, line_number)
        if not predicate(record):
            continue
        unit_id = record.get(id_key)
        _require(
            isinstance(unit_id, str) and unit_id,
            "REGISTRY_BINDING_INVALID",
            "registry unit id invalid",
            stage="POPULATION",
        )
        units.append(
            RegistryUnit(
                unit_kind=unit_kind,
                unit_id=unit_id,
                registry_file=registry_file,
                registry_file_sha256=file_sha256,
                registry_file_byte_count=len(data),
                line_number=line_number,
                line_sha256=sha256_bytes(line),
                record=record,
            )
        )
    return units


def _materialize_capsule(
    *,
    unit: RegistryUnit,
    repo_root: Path,
    source_base: Path,
    source_spec: Mapping[str, Any],
    model_record: Mapping[str, Any],
    visibility_sha256: str,
    stream_cache: dict[tuple[str, str], tuple[Path, EventStream, dict[str, Any], dict[str, Any]]],
    sink: DerivedArtifactSink,
) -> dict[str, Any]:
    row = unit.record
    _require(
        row.get("curated") is True and row.get("deployment_prediction") is False,
        "REGISTRY_BINDING_INVALID",
        "registry provenance flags invalid",
        stage="REGISTRY",
    )
    _require(
        row.get("protocol_version") == PROTOCOL_VERSION,
        "REGISTRY_BINDING_INVALID",
        "registry protocol mismatch",
        stage="REGISTRY",
    )
    frozen = row.get("frozen_capsule")
    _require(
        isinstance(frozen, Mapping),
        "REGISTRY_BINDING_INVALID",
        "frozen capsule projection missing",
        stage="REGISTRY",
    )
    locator = frozen.get("source_locator")
    _require(
        isinstance(locator, Mapping),
        "SOURCE_REFERENCE_UNRESOLVED",
        "source locator missing",
        stage="SOURCE",
    )
    run_relative = _safe_relative(locator.get("source_relative_run_path"), "source run")
    stream_relative = _safe_relative(locator.get("task_stream_relative_path"), "task stream")
    run_root = _safe_child(source_base, run_relative)
    cache_key = (str(run_root), stream_relative.as_posix())
    cutoff_seq = row["decision"]["request_cutoff"]["event_seq"]
    stream = _load_event_stream(
        run_root,
        stream_relative,
        expected_sha256=locator.get("task_stream_sha256"),
        expected_task_run_id=row["task"]["task_run_id"],
        max_seq=cutoff_seq,
        verify_full_identity=False,
    )
    chain = _resolve_pre_cutoff_chain(unit, stream)
    request = chain["request"]
    pre = chain["pre"]
    task_started = chain["task_started"]
    request_payload = request["payload"]
    blob_store = BlobStore(run_root)
    serializer = ArtifactSerializer(blob_store)

    sdk_ref = request_payload.get("sdk_arguments_snapshot_blob")
    _require_blob_ref(sdk_ref, code="BLOB_REFERENCE_INVALID")
    _verify_blob(blob_store, sdk_ref)
    try:
        graph = serializer.load_graph(sdk_ref)
        semantic_request = serializer.rehydrate(graph)
    except Exception as error:
        raise ReplayCapsuleError(
            "ARTIFACT_REHYDRATION_FAILED",
            "SDK argument graph could not be rehydrated",
            stage="REQUEST",
            error_type=type(error).__name__,
        ) from error
    _require(
        isinstance(semantic_request, Mapping),
        "ARTIFACT_REHYDRATION_FAILED",
        "rehydrated SDK arguments are not a mapping",
        stage="REQUEST",
    )
    semantic_request_sha256 = canonical_sha256(semantic_request)
    _require(
        semantic_request_sha256 == frozen.get("sdk_arguments_canonical_sha256"),
        "REQUEST_HASH_MISMATCH",
        "semantic request hash differs from frozen G1.1 projection",
        stage="REQUEST",
    )
    request_view = request_payload.get("request_view")
    _require(
        isinstance(request_view, Mapping),
        "REQUEST_VIEW_MISMATCH",
        "request view missing",
        stage="REQUEST",
    )
    request_view_sha256 = canonical_sha256(request_view)
    _require(
        request_view_sha256 == frozen.get("request_view_sha256"),
        "REQUEST_VIEW_MISMATCH",
        "request view hash differs from frozen G1.1 projection",
        stage="REQUEST",
    )
    externalized_request_images = _verify_request_view(
        semantic_request=semantic_request,
        request_view=request_view,
        blob_store=blob_store,
    )

    targets = _resolve_targets(unit, semantic_request, request_view)
    regions = _request_regions(
        history_family=row["history_family"],
        semantic_request=semantic_request,
        task_instruction=task_started["payload"]["task_goal"],
        targets=targets,
        request_images=request_payload.get("request_images"),
    )
    current_state = _current_state(
        row=row,
        chain=chain,
        stream=stream,
        regions=regions,
        blob_store=blob_store,
        semantic_request=semantic_request,
    )
    non_history_projection = _non_history_projection(semantic_request, regions)
    request_prefix = [event for event in stream.events if event["seq"] <= request["seq"]]
    request_prefix_sha256 = sha256_bytes(
        b"".join(canonical_json_line(event) for event in request_prefix)
    )
    task_projection = _task_parameter_projection(task_started["payload"])
    _require(
        g1_1_canonical_sha256(task_projection) == row["task"]["task_parameters_sha256"],
        "REGISTRY_BINDING_INVALID",
        "task parameter projection changed",
        stage="TASK",
    )
    _require(
        sha256_bytes(task_started["payload"]["task_goal"].encode("utf-8"))
        == row["task"]["task_instruction_sha256"],
        "REGISTRY_BINDING_INVALID",
        "task instruction changed",
        stage="TASK",
    )

    semantic_request_ref = sink.put_json(semantic_request, media_type="application/json")
    request_view_ref = sink.put_json(request_view, media_type="application/json")
    task_projection_ref = sink.put_bytes(
        canonical_json_line(task_projection), media_type="application/json"
    )
    observation = pre["payload"]["observation"]
    observation_ref = sink.put_json(observation, media_type="application/json")
    decoding_configuration = {
        key: _json_clone(value)
        for key, value in sorted(semantic_request.items())
        if key != "messages"
    }
    decoding_ref = sink.put_json(decoding_configuration, media_type="application/json")
    parser_implementation_ref = sink.put_json(
        model_record["parser_implementation"], media_type="application/json"
    )
    model_provider_provenance = _schema_model_provider_provenance(
        row=row,
        repo_root=repo_root,
        model_record=model_record,
        request_payload=request_payload,
        decoding_ref=decoding_ref,
    )
    replay_binding = _runtime_replay_binding(
        model_provider_provenance,
        parser_implementation_ref,
    )
    request_image_inventory = _schema_request_images(
        request_images=request_payload.get("request_images"),
        externalized_request_images=externalized_request_images,
        semantic_request=semantic_request,
        current_path=current_state["observation"]["screenshot"]["request_semantic_path"],
        run_id=run_root.name,
        blob_store=blob_store,
    )

    model_visible = {
        "semantic_request": {
            "request_event": _pre_cutoff_event_ref(request, stream),
            "authoritative_sdk_argument_graph_ref": _blob_content_ref(sdk_ref, run_root.name),
            "artifact_graph_version": ARTIFACT_GRAPH_VERSION,
            "canonical_semantic_request_ref": semantic_request_ref,
            "canonical_semantic_request_sha256": semantic_request_sha256,
            "canonical_semantic_request_byte_count": len(canonical_json_bytes(semantic_request)),
            "canonicalization": ("mobileworld.canonical-json/sorted-keys-utf8-no-nan/v1"),
            "inspectable_request_view_ref": request_view_ref,
            "inspectable_request_view_sha256": request_view_sha256,
            "request_images": request_image_inventory,
            "http_wire_bytes_status": "NOT_CAPTURED_BY_COLLECTOR_V1",
        },
        "region_partition": regions,
        "partition_sha256": canonical_sha256(regions),
        "non_history_projection_sha256": canonical_sha256(non_history_projection),
        "partition_complete": True,
        "partition_ambiguous": False,
    }
    non_history_envelope, non_history_refs = _schema_non_history_envelope(
        row=row,
        chain=chain,
        stream=stream,
        current_state=current_state,
        request=request,
        run_id=run_root.name,
        observation_ref=observation_ref,
        decoding_configuration=decoding_configuration,
        replay_binding=replay_binding,
        sink=sink,
    )
    treatment_surface = _schema_treatment_surface(
        row=row,
        semantic_request=semantic_request,
        targets=targets,
    )
    curator_only, curator_refs = _schema_curator_channels(
        row=row,
        chain=chain,
        stream=stream,
        targets=targets,
        task_instruction=task_started["payload"]["task_goal"],
        observation_ref=observation_ref,
        sink=sink,
    )
    start_manifest = _load_run_manifest(run_root, "manifest.start.json")
    final_manifest = _load_run_manifest(run_root, "manifest.final.json")
    if cache_key not in stream_cache:
        full_stream = _load_event_stream(
            run_root,
            stream_relative,
            expected_sha256=locator.get("task_stream_sha256"),
            expected_task_run_id=row["task"]["task_run_id"],
        )
        _validate_run_manifest_binding(
            run_root=run_root,
            stream=full_stream,
            start=start_manifest,
            final=final_manifest,
            task_run_id=row["task"]["task_run_id"],
        )
        stream_cache[cache_key] = (
            run_root,
            full_stream,
            start_manifest,
            final_manifest,
        )
    cached_run_root, full_stream, cached_start, cached_final = stream_cache[cache_key]
    full_prefix = full_stream.events[:cutoff_seq]
    prefix_event_ids = [event["event_id"] for event in stream.events]
    _require(
        cached_run_root == run_root
        and cached_start == start_manifest
        and cached_final == final_manifest
        and full_stream.sha256 == stream.sha256
        and full_stream.byte_count == stream.byte_count,
        "SOURCE_HASH_MISMATCH",
        "cached full stream differs from the validated pre-cutoff source binding",
        stage="SOURCE",
    )
    _require(
        len(stream.events) == cutoff_seq
        and tuple(full_prefix) == stream.events
        and {event_id: full_stream.line_sha256_by_id[event_id] for event_id in prefix_event_ids}
        == stream.line_sha256_by_id,
        "SOURCE_HASH_MISMATCH",
        "pre-cutoff event bytes changed between prefix validation and full-stream sealing",
        stage="SOURCE",
    )
    chain = chain | _resolve_post_action_chain(unit, full_stream, chain)
    stream = full_stream
    source_provenance, provenance_refs = _schema_source_provenance(
        unit=unit,
        row=row,
        source_base=source_base,
        source_spec=source_spec,
        run_root=run_root,
        stream=stream,
        chain=chain,
        start_manifest=start_manifest,
        final_manifest=final_manifest,
        model_provider_provenance=model_provider_provenance,
        task_projection_ref=task_projection_ref,
    )
    _require(
        replay_binding == _runtime_replay_binding(source_provenance, parser_implementation_ref),
        "REGISTRY_BINDING_INVALID",
        "pre-cutoff replay binding differs from sealed source provenance",
        stage="MODEL_CONFIG",
    )
    post_action, post_refs = _schema_post_action_audit(
        row=row,
        chain=chain,
        stream=stream,
        run_id=run_root.name,
        blob_store=blob_store,
        sink=sink,
    )
    runtime = {
        "model_visible": model_visible,
        "non_history_envelope": non_history_envelope,
        "treatment_surface": treatment_surface,
    }
    closure = _schema_artifact_closure(
        section_values={
            "SOURCE_PROVENANCE": [source_provenance, *provenance_refs],
            "FROZEN_MODEL_VISIBLE": [
                model_visible,
                *(_graph_blob_refs(graph, run_root.name, blob_store)),
            ],
            "FROZEN_NON_HISTORY_ENVELOPE": [
                non_history_envelope,
                *non_history_refs,
            ],
            "MUTABLE_HISTORY_TREATMENT": [treatment_surface],
            "CURATOR_ONLY": [curator_only, *curator_refs],
            "POST_ACTION_AUDIT_ONLY": [post_action, *post_refs],
        }
    )
    section_sha256s = {
        "source_provenance": canonical_sha256(source_provenance),
        "model_visible": canonical_sha256(model_visible),
        "non_history_envelope": canonical_sha256(non_history_envelope),
        "treatment_surface": canonical_sha256(treatment_surface),
        "curator_only": canonical_sha256(curator_only),
        "post_action_audit": canonical_sha256(post_action),
        "field_visibility": visibility_sha256,
        "artifact_closure": canonical_sha256(closure),
    }
    unit_record = _schema_unit(unit, row, run_root.name)
    capsule_identity = {
        "unit_id": unit.unit_id,
        "semantic_request_sha256": semantic_request_sha256,
        "request_prefix_sha256": request_prefix_sha256,
        "model_config_record_sha256": row["model_config_record_sha256"],
    }
    capsule_body = {
        "capsule_id": f"g1capsule-{canonical_sha256(capsule_identity)[:24]}",
        "protocol_version": PROTOCOL_VERSION,
        "portable_contract_version": PORTABLE_CONTRACT_VERSION,
        "issue": "ALE-321",
        "story": "G1.3",
        "curated": True,
        "deployment_prediction": False,
        "unit": unit_record,
        "source_provenance": source_provenance,
        "runtime": runtime,
        "curator_only": curator_only,
        "post_action_audit": post_action,
        "field_visibility": field_visibility_policy(),
        "artifact_closure": closure,
        "integrity_binding": {
            "validation_status": "VALID",
            "source_closure_sha256": _source_closure_sha256(closure),
            "runtime_projection_sha256": canonical_sha256(runtime),
            "section_sha256s": section_sha256s,
        },
        "safety": _safety_flags(),
    }
    _validate_visibility_boundary(capsule_body)
    body_sha256 = canonical_sha256(capsule_body)
    return {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "record_type": "g1_replay_capsule_envelope",
        "capsule_body_sha256": body_sha256,
        "capsule": capsule_body,
    }


def _load_event_stream(
    run_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_sha256: Any,
    expected_task_run_id: str,
    max_seq: int | None = None,
    verify_full_identity: bool = True,
) -> EventStream:
    _require_sha(expected_sha256, "task stream SHA")
    _require(
        max_seq is None
        or (isinstance(max_seq, int) and not isinstance(max_seq, bool) and max_seq >= 1),
        "REGISTRY_BINDING_INVALID",
        "event-stream validation cutoff is invalid",
        stage="REGISTRY",
    )
    _require(
        isinstance(verify_full_identity, bool),
        "REGISTRY_BINDING_INVALID",
        "event-stream full-identity validation flag is invalid",
        stage="REGISTRY",
    )
    path = _safe_child(run_root, relative_path)
    data = _read_regular(path)
    if verify_full_identity:
        _require(
            sha256_bytes(data) == expected_sha256,
            "SOURCE_HASH_MISMATCH",
            "task stream bytes differ from frozen locator",
            stage="SOURCE",
            json_path=f"/{relative_path.as_posix()}",
        )
    events: list[dict[str, Any]] = []
    event_by_id: dict[str, dict[str, Any]] = {}
    line_sha_by_id: dict[str, str] = {}
    expected_seq = 1
    prior_ids: set[str] = set()
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if max_seq is not None and line_number > max_seq:
            break
        _require(
            line.endswith(b"\n"),
            "RAW_EVENT_CHAIN_INVALID",
            "task stream line is not newline terminated",
            stage="SOURCE",
            json_path=f"/{relative_path.as_posix()}/{line_number}",
        )
        event = _load_canonical_line_bytes(line, path, line_number)
        try:
            validate_event_envelope(event)
        except Exception as error:
            raise ReplayCapsuleError(
                "RAW_EVENT_CHAIN_INVALID",
                "Collector event envelope invalid",
                stage="SOURCE",
                json_path=f"/{relative_path.as_posix()}/{line_number}",
                error_type=type(error).__name__,
            ) from error
        _require(
            event["task_run_id"] == expected_task_run_id
            and event["stream_id"] == expected_task_run_id,
            "RAW_EVENT_CHAIN_INVALID",
            "task event stream identity changed",
            stage="SOURCE",
        )
        _require(
            event["seq"] == expected_seq,
            "RAW_EVENT_CHAIN_INVALID",
            "task stream sequence is not contiguous",
            stage="SOURCE",
        )
        expected_seq += 1
        event_id = event["event_id"]
        _require(
            event_id not in event_by_id,
            "RAW_EVENT_CHAIN_INVALID",
            "duplicate event id",
            stage="SOURCE",
        )
        caused_by = event["caused_by_event_id"]
        _require(
            caused_by is None or caused_by in prior_ids,
            "RAW_EVENT_CHAIN_INVALID",
            "event cause is not earlier in the same task stream",
            stage="SOURCE",
            json_path=f"/{relative_path.as_posix()}/{line_number}/caused_by_event_id",
        )
        events.append(event)
        event_by_id[event_id] = event
        line_sha_by_id[event_id] = sha256_bytes(line)
        prior_ids.add(event_id)
    return EventStream(
        relative_path=relative_path.as_posix(),
        sha256=expected_sha256,
        byte_count=len(data),
        events=tuple(events),
        line_sha256_by_id=line_sha_by_id,
        event_by_id=event_by_id,
    )


def _load_run_manifest(run_root: Path, name: str) -> dict[str, Any]:
    path = run_root / name
    return _load_canonical_object(path)


def _validate_run_manifest_binding(
    *,
    run_root: Path,
    stream: EventStream,
    start: Mapping[str, Any],
    final: Mapping[str, Any],
    task_run_id: str,
) -> None:
    _require(
        start.get("run_id") == run_root.name
        and final.get("run_id") == run_root.name
        and start.get("raw_schema_version") == "mobileworld.audit.event/v1"
        and final.get("raw_schema_version") == "mobileworld.audit.event/v1",
        "SOURCE_HASH_MISMATCH",
        "run manifest identity changed",
        stage="SOURCE",
    )
    summary = [
        record
        for record in final.get("task_streams", [])
        if isinstance(record, Mapping) and record.get("task_run_id") == task_run_id
    ]
    _require(
        len(summary) == 1,
        "SOURCE_REFERENCE_UNRESOLVED",
        "task stream manifest summary is not unique",
        stage="SOURCE",
    )
    record = summary[0]
    _require(
        record.get("relative_path") == stream.relative_path
        and record.get("sha256") == stream.sha256
        and record.get("byte_count") == stream.byte_count,
        "SOURCE_HASH_MISMATCH",
        "task stream manifest summary changed",
        stage="SOURCE",
    )
    _require(
        record.get("capture_complete") is True
        and record.get("missing_artifacts") == []
        and record.get("collector_error_event_ids") == [],
        "SOURCE_REFERENCE_UNRESOLVED",
        "selected task stream is not independently capture-complete",
        stage="SOURCE",
    )


def _resolve_pre_cutoff_chain(unit: RegistryUnit, stream: EventStream) -> dict[str, Any]:
    row = unit.record
    decision = row["decision"]
    request = _event(stream, decision["request_event_id"], "model_request")
    _require(
        request["seq"] == decision["request_cutoff"]["event_seq"]
        and request["monotonic_ns"] == decision["request_cutoff"]["monotonic_ns"]
        and request["wall_time"] == decision["request_cutoff"]["wall_time"],
        "RAW_EVENT_CHAIN_INVALID",
        "request cutoff changed",
        stage="CHAIN",
    )
    pre = _event(stream, request["caused_by_event_id"], "step_started")
    _require(
        pre["payload"].get("step_id") == decision["step_id"]
        and pre["payload"].get("step_index") == decision["target_step"],
        "RAW_EVENT_CHAIN_INVALID",
        "target step binding changed",
        stage="CHAIN",
    )
    last_transition = _event(stream, pre["caused_by_event_id"], "transition_completed")
    _require(
        last_transition["payload"].get("post_observation") == pre["payload"].get("observation"),
        "STATE_HASH_MISMATCH",
        "pre-call state differs from preceding causal transition",
        stage="CHAIN",
    )
    task_started = [event for event in stream.events if event["event_type"] == "task_started"]
    _require(
        len(task_started) == 1,
        "RAW_EVENT_CHAIN_INVALID",
        "task_started event is not unique in the pre-cutoff prefix",
        stage="CHAIN",
    )
    return {
        "task_started": task_started[0],
        "last_transition": last_transition,
        "pre": pre,
        "request": request,
    }


def _resolve_post_action_chain(
    unit: RegistryUnit,
    stream: EventStream,
    pre_chain: Mapping[str, Any],
) -> dict[str, Any]:
    row = unit.record
    decision = row["decision"]
    request = pre_chain["request"]
    pre = pre_chain["pre"]
    responses = [
        event
        for event in stream.events
        if event["event_type"] == "model_response"
        and event["caused_by_event_id"] == request["event_id"]
        and event["payload"].get("request_id") == request["payload"].get("request_id")
        and event["payload"].get("model_call_id") == request["payload"].get("model_call_id")
    ]
    _require(
        len(responses) == 1,
        "ORIGINAL_RESPONSE_UNRESOLVED",
        "target natural response is not unique",
        stage="POST_ACTION",
    )
    response = responses[0]
    decision_event = _event(stream, decision["decision_event_id"], "agent_decision")
    _require(
        decision_event["caused_by_event_id"] == response["event_id"]
        and request["payload"].get("request_id") == decision["request_id"]
        and request["payload"].get("model_call_id") == decision["model_call_id"]
        and decision_event["payload"].get("source_model_call_ids") == [decision["model_call_id"]],
        "ORIGINAL_ACTION_UNRESOLVED",
        "target response/decision/request chain changed",
        stage="POST_ACTION",
    )
    children = [
        event
        for event in stream.events
        if event["caused_by_event_id"] == decision_event["event_id"]
    ]
    execution: dict[str, Any] | None
    terminal: dict[str, Any]
    if len(children) == 1 and children[0]["event_type"] == "action_execution_started":
        execution = children[0]
        terminals = [
            event
            for event in stream.events
            if event["caused_by_event_id"] == execution["event_id"]
            and event["event_type"] in {"transition_completed", "transition_failed"}
        ]
        _require(
            len(terminals) == 1,
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "target execution terminal is not unique",
            stage="POST_ACTION",
        )
        terminal = terminals[0]
        _require(
            terminal["event_type"] == "transition_completed"
            and terminal["payload"].get("pre_observation_event_id") == pre["event_id"],
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "target completed transition binding changed",
            stage="POST_ACTION",
        )
    elif len(children) == 1 and children[0]["event_type"] == "transition_not_executed":
        execution = None
        terminal = children[0]
        _require(
            terminal["payload"].get("reason") == "terminal_action",
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "non-executed target is not a terminal action",
            stage="POST_ACTION",
        )
    else:
        raise ReplayCapsuleError(
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "target decision terminal shape is ambiguous",
            stage="POST_ACTION",
            child_types=sorted(event["event_type"] for event in children),
        )
    return {
        "response": response,
        "decision": decision_event,
        "execution": execution,
        "terminal": terminal,
    }


def _resolve_target_chain(unit: RegistryUnit, stream: EventStream) -> dict[str, Any]:
    """Resolve a full natural chain while preserving the two-phase implementation API."""

    pre_chain = _resolve_pre_cutoff_chain(unit, stream)
    return pre_chain | _resolve_post_action_chain(unit, stream, pre_chain)


def _verify_request_view(
    *, semantic_request: Mapping[str, Any], request_view: Mapping[str, Any], blob_store: BlobStore
) -> list[dict[str, Any]]:
    externalized_request_images: list[dict[str, Any]] = []

    def visit(authoritative: Any, projected: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(projected, Mapping) and set(projected) == {"$externalized_data_url"}:
            descriptor = projected["$externalized_data_url"]
            _require(
                isinstance(descriptor, Mapping)
                and isinstance(authoritative, str)
                and authoritative.startswith("data:image/"),
                "REQUEST_VIEW_MISMATCH",
                "externalized image data URL projection shape invalid",
                stage="REQUEST",
            )
            original_ref = descriptor.get("original_text_blob")
            _require_blob_ref(original_ref, code="BLOB_REFERENCE_INVALID")
            original = _read_verified_blob(blob_store, original_ref)
            _require(
                original.decode("utf-8") == authoritative,
                "REQUEST_VIEW_MISMATCH",
                "externalized data URL cannot restore exact original text",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
            content_ref = descriptor.get("content_blob")
            _require_blob_ref(content_ref, code="BLOB_REFERENCE_INVALID")
            _verify_blob(blob_store, content_ref)
            _require(
                descriptor.get("content_path") == _dot_path(path),
                "REQUEST_VIEW_MISMATCH",
                "externalized content path changed",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
            externalized_request_images.append(
                {
                    "content_path": _dot_path(path),
                    "semantic_request_path": list(path),
                    "content_blob": _json_clone(content_ref),
                    "original_text_blob": _json_clone(original_ref),
                }
            )
            return
        _require(
            not (isinstance(authoritative, str) and authoritative.startswith("data:image/")),
            "REQUEST_VIEW_MISMATCH",
            "semantic request image data URL was not externalized into the request view",
            stage="REQUEST",
            json_path=_json_pointer(path),
        )
        if isinstance(projected, Mapping):
            _require(
                isinstance(authoritative, Mapping) and set(authoritative) == set(projected),
                "REQUEST_VIEW_MISMATCH",
                "request view mapping differs from semantic request",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
            for key in projected:
                visit(authoritative[key], projected[key], (*path, key))
            return
        if isinstance(projected, list):
            _require(
                isinstance(authoritative, list) and len(authoritative) == len(projected),
                "REQUEST_VIEW_MISMATCH",
                "request view list differs from semantic request",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
            for index, value in enumerate(projected):
                visit(authoritative[index], value, (*path, index))
            return
        _require(
            authoritative == projected,
            "REQUEST_VIEW_MISMATCH",
            "request view scalar differs from semantic request",
            stage="REQUEST",
            json_path=_json_pointer(path),
        )

    visit(semantic_request, request_view, ())
    externalized_request_images.sort(
        key=lambda record: _typed_path_sort_key(record["semantic_request_path"])
    )
    _require(
        len(externalized_request_images)
        == len({record["content_path"] for record in externalized_request_images}),
        "REQUEST_VIEW_MISMATCH",
        "request view externalizes one request path more than once",
        stage="REQUEST",
    )
    return externalized_request_images


def _resolve_targets(
    unit: RegistryUnit,
    semantic_request: Mapping[str, Any],
    request_view: Mapping[str, Any],
) -> list[dict[str, Any]]:
    row = unit.record
    histories = row.get("target_histories")
    frozen_spans = unit.record["frozen_capsule"].get("resolved_target_spans")
    _require(
        isinstance(histories, list)
        and histories
        and isinstance(frozen_spans, list)
        and len(histories) == len(frozen_spans),
        "TARGET_SPAN_UNRESOLVED",
        "target history/frozen span cardinality changed",
        stage="TARGET",
    )
    frozen_by_candidate = {
        span.get("target_set_entry", {}).get("candidate_id"): span for span in frozen_spans
    }
    _require(
        len(frozen_by_candidate) == len(frozen_spans),
        "TARGET_SPAN_AMBIGUOUS",
        "frozen target candidate binding is ambiguous",
        stage="TARGET",
    )
    targets: list[dict[str, Any]] = []
    intervals: defaultdict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)
    for history in histories:
        _require(
            isinstance(history, Mapping),
            "TARGET_SPAN_UNRESOLVED",
            "target history is not a mapping",
            stage="TARGET",
        )
        candidate_id = history.get("candidate_id")
        frozen = frozen_by_candidate.get(candidate_id)
        _require(
            isinstance(candidate_id, str) and isinstance(frozen, Mapping),
            "TARGET_SPAN_UNRESOLVED",
            "target candidate is not bound to frozen span",
            stage="TARGET",
        )
        registry_path = history.get("request_path")
        path = _semantic_record_path(registry_path, row["history_family"])
        container = _get_path(semantic_request, path)
        projected_container = _get_path(request_view, path)
        _require(
            isinstance(container, str)
            and isinstance(projected_container, str)
            and container == projected_container,
            "TARGET_SPAN_COORDINATE_MISMATCH",
            "target container is not exact text in both request coordinate spaces",
            stage="TARGET",
            json_path=_json_pointer(path),
        )
        start = history.get("char_start")
        end = history.get("char_end")
        byte_start = history.get("utf8_byte_start")
        byte_end = history.get("utf8_byte_end")
        _require_int_range(start, end, len(container), "target char offsets")
        _require(
            isinstance(byte_start, int)
            and not isinstance(byte_start, bool)
            and isinstance(byte_end, int)
            and not isinstance(byte_end, bool)
            and byte_start == len(container[:start].encode("utf-8"))
            and byte_end == len(container[:end].encode("utf-8")),
            "TARGET_SPAN_COORDINATE_MISMATCH",
            "target UTF-8 offsets do not match character offsets",
            stage="TARGET",
            json_path=_json_pointer(path),
        )
        exact = container[start:end]
        span_sha = sha256_bytes(exact.encode("utf-8"))
        _require(
            span_sha == history.get("span_sha256")
            and sha256_bytes(container.encode("utf-8")) == history.get("source_record_sha256"),
            "TARGET_SPAN_HASH_MISMATCH",
            "target or container hash changed",
            stage="TARGET",
            json_path=_json_pointer(path),
        )
        _require(
            frozen.get("record_path") == registry_path
            and frozen.get("char_start") == start
            and frozen.get("char_end") == end
            and frozen.get("utf8_byte_start") == byte_start
            and frozen.get("utf8_byte_end") == byte_end
            and frozen.get("span_sha256") == span_sha,
            "TARGET_SPAN_COORDINATE_MISMATCH",
            "raw request target differs from frozen G1.1 projection",
            stage="TARGET",
        )
        edit_status = history.get("edit_span_status")
        focal = history.get("focal_edit_spans")
        _require(
            edit_status in {"G1_1_FROZEN", "G1_6_PENDING"} and isinstance(focal, list),
            "TARGET_SPAN_UNRESOLVED",
            "target edit status invalid",
            stage="TARGET",
        )
        if row["history_family"] == "RAW_REPLAY":
            _require(
                edit_status == "G1_6_PENDING" and focal == [],
                "TARGET_SPAN_COORDINATE_MISMATCH",
                "raw replay edit span was promoted before G1.6",
                stage="TARGET",
            )
        else:
            _require(
                edit_status == "G1_1_FROZEN" and len(focal) == 1,
                "TARGET_SPAN_COORDINATE_MISMATCH",
                "flat-progress focal span is not frozen",
                stage="TARGET",
            )
        focal_out: list[dict[str, Any]] = []
        for span in focal:
            focal_start = span.get("char_start")
            focal_end = span.get("char_end")
            _require_int_range(focal_start, focal_end, len(container), "focal char offsets")
            _require(
                start <= focal_start < focal_end <= end
                and span.get("utf8_byte_start") == len(container[:focal_start].encode("utf-8"))
                and span.get("utf8_byte_end") == len(container[:focal_end].encode("utf-8"))
                and span.get("span_sha256")
                == sha256_bytes(container[focal_start:focal_end].encode("utf-8")),
                "TARGET_SPAN_COORDINATE_MISMATCH",
                "focal span does not resolve exactly",
                stage="TARGET",
            )
            focal_out.append(_source_span(path, container, focal_start, focal_end))
            intervals[path].append((focal_start, focal_end))
        envelope = history.get("curation_envelope")
        if envelope is not None:
            _validate_curation_envelope(container, envelope)
        curation_out = (
            _source_span(path, container, envelope["char_start"], envelope["char_end"])
            if envelope is not None
            else None
        )
        target_set_entry = frozen.get("target_set_entry")
        _require(
            isinstance(target_set_entry, Mapping),
            "TARGET_SPAN_UNRESOLVED",
            "frozen target-set entry missing",
            stage="TARGET",
        )
        targets.append(
            {
                "candidate_id": candidate_id,
                "provenance_confidence": history.get("provenance_confidence"),
                "source_steps": _json_clone(history.get("source_steps")),
                "registry_request_path": registry_path,
                "semantic_request_container_path": list(path),
                # Internal aliases used by the independent region validator.
                "semantic_container_path": list(path),
                "message_index": path[1],
                "content_block_index": path[3] if len(path) > 4 else None,
                "record_index": history["source_steps"][-1] - 1,
                "record_identity_sha256": target_set_entry.get("record_identity_sha256"),
                "container_sha256": sha256_bytes(container.encode("utf-8")),
                "record_sha256": sha256_bytes(container.encode("utf-8")),
                "exposure_span": _source_span(path, container, start, end),
                "exposure": _source_span(path, container, start, end),
                "edit_span_status": edit_status,
                "focal_edit_spans": focal_out,
                "curation_envelope": curation_out,
                "transform_binding": history.get("transform_binding"),
            }
        )
    targets.sort(
        key=lambda value: (
            _typed_path_sort_key(value["semantic_container_path"]),
            value["exposure_span"]["char_start"],
            value["exposure_span"]["char_end"],
            value["candidate_id"],
        )
    )
    for path, spans in intervals.items():
        ordered = sorted(spans)
        _require(
            all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
            "TARGET_SET_OVERLAP",
            "focal edit spans overlap",
            stage="TARGET",
            json_path=_json_pointer(path),
        )
    return targets


def _tool_protocol_spans(source: str) -> list[tuple[int, int]]:
    """Return every exact, non-nested ``<tool_call>`` protocol span."""

    opening = "<tool_call>"
    closing = "</tool_call>"
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = source.find(opening, cursor)
        stray_close = source.find(closing, cursor)
        if start < 0:
            _require(
                stray_close < 0,
                "REQUEST_PARTITION_AMBIGUOUS",
                "raw-replay assistant text has an unmatched tool-call closing tag",
                stage="REGIONS",
            )
            break
        _require(
            stray_close < 0 or start < stray_close,
            "REQUEST_PARTITION_AMBIGUOUS",
            "raw-replay assistant text has an unmatched tool-call closing tag",
            stage="REGIONS",
        )
        close_start = source.find(closing, start + len(opening))
        _require(
            close_start >= 0 and source.find(opening, start + len(opening), close_start) < 0,
            "REQUEST_PARTITION_AMBIGUOUS",
            "raw-replay assistant tool-call protocol tags are missing or nested",
            stage="REGIONS",
        )
        end = close_start + len(closing)
        spans.append((start, end))
        cursor = end
    return spans


def _request_regions(
    *,
    history_family: str,
    semantic_request: Mapping[str, Any],
    task_instruction: str,
    targets: Sequence[Mapping[str, Any]],
    request_images: Any,
) -> list[dict[str, Any]]:
    messages = semantic_request.get("messages")
    _require(
        isinstance(messages, list) and len(messages) >= 2,
        "REQUEST_PARTITION_INCOMPLETE",
        "semantic request messages missing",
        stage="REGIONS",
    )
    provider_bindings = [
        _whole_value_binding(
            (key,),
            semantic_request[key],
            role="PROVIDER_PARAMETER",
            visibility_class="FROZEN_NON_HISTORY_ENVELOPE",
        )
        for key in sorted(semantic_request)
        if key != "messages"
    ]
    if history_family == "FLAT_PROGRESS":
        _require(
            len(messages) == 2
            and messages[0].get("role") == "system"
            and messages[1].get("role") == "user"
            and isinstance(messages[0].get("content"), list)
            and len(messages[0]["content"]) == 1
            and isinstance(messages[1].get("content"), list)
            and len(messages[1]["content"]) == 2,
            "REQUEST_PARTITION_AMBIGUOUS",
            "flat-progress host request shape changed",
            stage="REGIONS",
        )
        system_text = messages[0]["content"][0].get("text")
        user_text = messages[1]["content"][0].get("text")
        _require(
            isinstance(system_text, str) and isinstance(user_text, str),
            "REQUEST_PARTITION_INCOMPLETE",
            "flat-progress text blocks missing",
            stage="REGIONS",
        )
        system_protocol_binding = _text_binding(
            ("messages", 0, "content", 0, "text"),
            system_text,
            0,
            len(system_text),
            role="TOOL_PROTOCOL_AND_SYSTEM_SHELL",
            visibility_class="FROZEN_MODEL_VISIBLE",
        )
        _require(
            user_text.count(task_instruction) == 1,
            "REQUEST_PARTITION_AMBIGUOUS",
            "task instruction is not unique in flat-progress text",
            stage="REGIONS",
        )
        task_start = user_text.index(task_instruction)
        task_end = task_start + len(task_instruction)
        marker = "Task progress (You have done the following operation on the current device): "
        _require(
            user_text.count(marker) == 1,
            "REQUEST_PARTITION_AMBIGUOUS",
            "flat-progress history marker changed",
            stage="REGIONS",
        )
        marker_start = user_text.index(marker)
        history_start = marker_start + len(marker)
        history_end = len(user_text)
        _require(
            task_end <= marker_start < history_start < history_end,
            "REQUEST_PARTITION_AMBIGUOUS",
            "flat-progress task, history marker, and history content are not ordered uniquely",
            stage="REGIONS",
        )
        user_text_path = ("messages", 1, "content", 0, "text")
        task_bindings = [
            _whole_value_binding(
                ("messages", 1, "role"),
                messages[1]["role"],
                role="USER_MESSAGE_ROLE",
                visibility_class="FROZEN_MODEL_VISIBLE",
            ),
            _whole_value_binding(
                ("messages", 1, "content", 0, "type"),
                messages[1]["content"][0]["type"],
                role="TEXT_BLOCK_TYPE",
                visibility_class="FROZEN_MODEL_VISIBLE",
            ),
        ]
        for start, end, role in (
            (0, task_start, "QWEN_USER_PREFIX"),
            (task_start, task_end, "TASK_INSTRUCTION"),
            (task_end, history_start, "QWEN_TASK_HISTORY_SHELL"),
        ):
            if start < end:
                task_bindings.append(
                    _text_binding(
                        user_text_path,
                        user_text,
                        start,
                        end,
                        role=role,
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    )
                )
        regions = [
            _region(
                "system",
                "SYSTEM",
                "PRESENT",
                [
                    _whole_value_binding(
                        ("messages", 0, "role"),
                        messages[0]["role"],
                        role="SYSTEM_MESSAGE_ROLE",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    ),
                    _whole_value_binding(
                        ("messages", 0, "content", 0, "type"),
                        messages[0]["content"][0]["type"],
                        role="SYSTEM_TEXT_BLOCK_TYPE",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    ),
                    system_protocol_binding,
                ],
            ),
            _region(
                "task",
                "TASK",
                "COLOCATED",
                task_bindings,
            ),
            _region(
                "history",
                "HISTORY",
                "COLOCATED",
                [
                    _text_binding(
                        ("messages", 1, "content", 0, "text"),
                        user_text,
                        history_start,
                        history_end,
                        role="FLAT_PROGRESS_HISTORY",
                        visibility_class="MUTABLE_HISTORY_TREATMENT",
                    )
                ],
            ),
            _region(
                "current_observation",
                "CURRENT_OBSERVATION",
                "PRESENT",
                [
                    _whole_value_binding(
                        ("messages", 1, "content", 1),
                        messages[1]["content"][1],
                        role="CURRENT_SCREENSHOT",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    )
                ],
            ),
            _region(
                "tool_protocol",
                "TOOL_PROTOCOL",
                "COLOCATED",
                [system_protocol_binding],
                ownership_role="PROTECTED_OVERLAY",
            ),
            _region(
                "provider_control",
                "PROVIDER_CONTROL",
                "PRESENT",
                provider_bindings,
            ),
        ]
        expected_current_path = ("messages", 1, "content", 1, "image_url", "url")
    else:
        _require(
            history_family == "RAW_REPLAY"
            and messages[0].get("role") == "system"
            and messages[1].get("role") == "user",
            "REQUEST_PARTITION_AMBIGUOUS",
            "raw-replay host request shape changed",
            stage="REGIONS",
        )
        current_index = len(messages) - 1
        current = messages[current_index]
        _require(
            current.get("role") == "user"
            and isinstance(current.get("content"), list)
            and len(current["content"]) == 1
            and current["content"][0].get("type") == "image_url",
            "CURRENT_OBSERVATION_UNRESOLVED",
            "raw-replay current observation is not the last unique user image block",
            stage="REGIONS",
        )
        task_content = messages[1].get("content")
        _require(
            isinstance(task_content, list)
            and len(task_content) == 1
            and task_content[0].get("text") == task_instruction,
            "REQUEST_PARTITION_AMBIGUOUS",
            "raw-replay task message changed",
            stage="REGIONS",
        )
        history_bindings: list[dict[str, Any]] = []
        historical_tool_bindings: list[dict[str, Any]] = []
        for index, message in enumerate(messages[2:current_index], start=2):
            _require(
                isinstance(message, Mapping) and message.get("role") in {"assistant", "user"},
                "REQUEST_PARTITION_AMBIGUOUS",
                "raw-replay history contains an unsupported message shape or role",
                stage="REGIONS",
                json_path=_json_pointer(("messages", index)),
            )
            history_bindings.append(
                _whole_value_binding(
                    ("messages", index, "role"),
                    message["role"],
                    role="HISTORY_MESSAGE_ROLE",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                )
            )
            content = message.get("content")
            if message["role"] == "user":
                _require(
                    isinstance(content, list),
                    "REQUEST_PARTITION_AMBIGUOUS",
                    "raw-replay historical user observation is not a content-block list",
                    stage="REGIONS",
                    json_path=_json_pointer(("messages", index, "content")),
                )
                history_bindings.append(
                    _whole_value_binding(
                        ("messages", index, "content"),
                        content,
                        role="HISTORICAL_USER_OBSERVATION",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    )
                )
                continue
            _require(
                isinstance(content, str),
                "REQUEST_PARTITION_AMBIGUOUS",
                "raw-replay assistant history content is not exact text",
                stage="REGIONS",
                json_path=_json_pointer(("messages", index, "content")),
            )
            content_path = ("messages", index, "content")
            tool_spans = _tool_protocol_spans(content)
            if not content:
                history_bindings.append(
                    _whole_value_binding(
                        content_path,
                        content,
                        role="ASSISTANT_HISTORY_CONTENT",
                        visibility_class="MUTABLE_HISTORY_TREATMENT",
                    )
                )
                continue
            cursor = 0
            for tool_start, tool_end in tool_spans:
                if cursor < tool_start:
                    history_bindings.append(
                        _text_binding(
                            content_path,
                            content,
                            cursor,
                            tool_start,
                            role="ASSISTANT_HISTORY_CONTENT",
                            visibility_class="MUTABLE_HISTORY_TREATMENT",
                        )
                    )
                tool_binding = _text_binding(
                    content_path,
                    content,
                    tool_start,
                    tool_end,
                    role="HISTORICAL_TOOL_CALL_PROTOCOL",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                )
                history_bindings.append(tool_binding)
                historical_tool_bindings.append(tool_binding)
                cursor = tool_end
            if cursor < len(content):
                history_bindings.append(
                    _text_binding(
                        content_path,
                        content,
                        cursor,
                        len(content),
                        role="ASSISTANT_HISTORY_CONTENT",
                        visibility_class="MUTABLE_HISTORY_TREATMENT",
                    )
                )
        _require(
            history_bindings,
            "REQUEST_PARTITION_INCOMPLETE",
            "raw-replay history is empty",
            stage="REGIONS",
        )
        system_content = messages[0].get("content")
        _require(
            isinstance(system_content, str) and bool(system_content),
            "REQUEST_PARTITION_INCOMPLETE",
            "raw-replay system protocol text is missing",
            stage="REGIONS",
        )
        system_protocol_binding = _text_binding(
            ("messages", 0, "content"),
            system_content,
            0,
            len(system_content),
            role="SYSTEM_ACTION_PROTOCOL",
            visibility_class="FROZEN_MODEL_VISIBLE",
        )
        tool_bindings = [system_protocol_binding, *historical_tool_bindings]
        regions = [
            _region(
                "system",
                "SYSTEM",
                "PRESENT",
                [
                    _whole_value_binding(
                        ("messages", 0, "role"),
                        messages[0]["role"],
                        role="SYSTEM_MESSAGE_ROLE",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    ),
                    system_protocol_binding,
                ],
            ),
            _region(
                "task",
                "TASK",
                "PRESENT",
                [
                    _whole_value_binding(
                        ("messages", 1),
                        messages[1],
                        role="TASK_INSTRUCTION",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    )
                ],
            ),
            _region(
                "history",
                "HISTORY",
                "PRESENT",
                history_bindings,
            ),
            _region(
                "current_observation",
                "CURRENT_OBSERVATION",
                "PRESENT",
                [
                    _whole_value_binding(
                        ("messages", current_index, "content", 0),
                        current["content"][0],
                        role="CURRENT_SCREENSHOT",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    ),
                    _whole_value_binding(
                        ("messages", current_index, "role"),
                        current["role"],
                        role="CURRENT_OBSERVATION_ROLE",
                        visibility_class="FROZEN_MODEL_VISIBLE",
                    ),
                ],
            ),
            _region(
                "tool_protocol",
                "TOOL_PROTOCOL",
                "COLOCATED",
                tool_bindings,
                ownership_role="PROTECTED_OVERLAY",
            ),
            _region(
                "provider_control",
                "PROVIDER_CONTROL",
                "PRESENT",
                provider_bindings,
            ),
        ]
        expected_current_path = (
            "messages",
            current_index,
            "content",
            0,
            "image_url",
            "url",
        )
    _require(
        isinstance(request_images, list),
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "request image inventory missing",
        stage="REGIONS",
    )
    current_matches = [
        image
        for image in request_images
        if isinstance(image, Mapping)
        and image.get("content_path") == _dot_path(expected_current_path)
    ]
    _require(
        len(current_matches) == 1,
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "current screenshot request coordinate is not unique",
        stage="REGIONS",
    )
    _require(
        {region["kind"] for region in regions}
        == {
            "SYSTEM",
            "TASK",
            "HISTORY",
            "CURRENT_OBSERVATION",
            "TOOL_PROTOCOL",
            "PROVIDER_CONTROL",
        },
        "REQUEST_PARTITION_INCOMPLETE",
        "request semantic region census incomplete",
        stage="REGIONS",
    )
    _validate_semantic_request_partition(semantic_request, regions)
    _validate_targets_inside_history(targets, regions)
    return regions


def _current_state(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    stream: EventStream,
    regions: Sequence[Mapping[str, Any]],
    blob_store: BlobStore,
    semantic_request: Mapping[str, Any],
) -> dict[str, Any]:
    pre = chain["pre"]
    observation = pre["payload"].get("observation")
    _require(
        isinstance(observation, Mapping),
        "CURRENT_OBSERVATION_UNRESOLVED",
        "pre-call observation missing",
        stage="STATE",
    )
    screenshot = observation.get("screenshot")
    _require(
        isinstance(screenshot, Mapping),
        "CURRENT_OBSERVATION_UNRESOLVED",
        "pre-call screenshot missing",
        stage="STATE",
    )
    pixel_ref = screenshot.get("pixel_blob")
    _require_blob_ref(pixel_ref, code="BLOB_REFERENCE_INVALID")
    _verify_blob(blob_store, pixel_ref)
    source_ref = screenshot.get("source_blob")
    if source_ref is not None:
        _require_blob_ref(source_ref, code="BLOB_REFERENCE_INVALID")
        _verify_blob(blob_store, source_ref)
    current_region = next(region for region in regions if region["kind"] == "CURRENT_OBSERVATION")
    current_binding = current_region["bindings"][0]
    current_block = _get_path(semantic_request, tuple(current_binding["path"]))
    data_url = current_block["image_url"]["url"]
    _require(
        isinstance(data_url, str),
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "current semantic request image is not an exact data URL",
        stage="STATE",
    )
    request_path = (*tuple(current_binding["path"]), "image_url", "url")
    request_images = chain["request"]["payload"].get("request_images")
    matches = [
        image
        for image in request_images
        if image.get("content_path") == _dot_path(request_path)
        and image.get("content_blob", {}).get("digest") == pixel_ref["digest"]
    ]
    _require(
        len(matches) == 1,
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "current screenshot path/blob binding changed",
        stage="STATE",
    )
    original_ref = matches[0].get("original_text_blob")
    _require_blob_ref(original_ref, code="BLOB_REFERENCE_INVALID")
    _require(
        _read_verified_blob(blob_store, original_ref).decode("utf-8") == data_url,
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "current screenshot data URL cannot be restored",
        stage="STATE",
    )
    frozen_current = row["frozen_capsule"].get("current_gui_blob")
    _require(
        frozen_current == pixel_ref,
        "STATE_HASH_MISMATCH",
        "current GUI blob differs from frozen G1.1 projection",
        stage="STATE",
    )
    accessibility = observation.get("accessibility_tree")
    _require(
        accessibility is None,
        "STATE_HASH_MISMATCH",
        "unexpected UI-tree representation requires a new contract version",
        stage="STATE",
    )
    return {
        "step_started_event": _event_ref(chain["pre"], stream),
        "observation_sha256": canonical_sha256(observation),
        "observation": {
            "screenshot": {
                "pixel_blob": _json_clone(pixel_ref),
                "source_blob": _json_clone(source_ref),
                "width": screenshot.get("width"),
                "height": screenshot.get("height"),
                "mode": screenshot.get("mode"),
                "request_semantic_path": list(request_path),
                "request_content_path": _dot_path(request_path),
                "original_text_blob": _json_clone(original_ref),
                "selected_by_host_coordinate_not_digest_uniqueness": True,
            },
            "ui_tree": {"status": "ABSENT_AT_CAPTURE", "value": None},
            "tool_call": _json_clone(observation.get("tool_call")),
            "ask_user_response": _json_clone(observation.get("ask_user_response")),
        },
        "last_causal_transition": {
            "event": _event_ref(chain["last_transition"], stream),
            "post_observation_sha256": canonical_sha256(
                chain["last_transition"]["payload"]["post_observation"]
            ),
            "strictly_before_request": chain["last_transition"]["seq"] < chain["request"]["seq"],
        },
        "request_cutoff": {
            "event_id": chain["request"]["event_id"],
            "event_seq": chain["request"]["seq"],
            "wall_time": chain["request"]["wall_time"],
            "monotonic_ns": chain["request"]["monotonic_ns"],
        },
    }


def _source_provenance(
    *,
    unit: RegistryUnit,
    row: Mapping[str, Any],
    run_root: Path,
    stream: EventStream,
    chain: Mapping[str, Any],
    source_spec: Mapping[str, Any],
    source_base: Path,
    start_manifest: Mapping[str, Any],
    final_manifest: Mapping[str, Any],
    request_prefix_sha256: str,
) -> dict[str, Any]:
    curated_relative = _safe_relative(source_spec.get("curated_manifest"), "curated manifest")
    curated_path = _safe_child(source_base, curated_relative)
    curated_bytes = _read_regular(curated_path)
    _require(
        sha256_bytes(curated_bytes) == source_spec.get("curated_manifest_sha256"),
        "SOURCE_HASH_MISMATCH",
        "curated source manifest changed",
        stage="SOURCE",
    )
    validation_path = curated_path.parent / "validation_report.json"
    validation_bytes = _read_regular(validation_path)
    validation = _load_canonical_object_bytes(validation_bytes, validation_path)
    _require(
        validation.get("valid") is True,
        "SOURCE_REFERENCE_UNRESOLVED",
        "curated transitive validation receipt is not valid",
        stage="SOURCE",
    )
    start_path = run_root / "manifest.start.json"
    final_path = run_root / "manifest.final.json"
    task = row["task"]
    pre_events = [
        chain["task_started"],
        chain["last_transition"],
        chain["pre"],
        chain["request"],
    ]
    return {
        "collector_schema_version": "mobileworld.audit.event/v1",
        "source_id": row["frozen_capsule"]["source_locator"]["source_id"],
        "source_run_id": run_root.name,
        "task_stream_relative_path": stream.relative_path,
        "task_run_id": task["task_run_id"],
        "full_task_stream": {
            "sha256": stream.sha256,
            "byte_count": stream.byte_count,
            "visibility": "POST_ACTION_AUDIT_ONLY",
        },
        "runtime_prefix_through_request": {
            "last_event_id": chain["request"]["event_id"],
            "last_event_seq": chain["request"]["seq"],
            "sha256": request_prefix_sha256,
            "event_count": chain["request"]["seq"],
        },
        "pre_request_event_chain": [_event_ref(event, stream) for event in pre_events],
        "run_manifests": {
            "start": {
                "sha256": sha256_bytes(_read_regular(start_path)),
                "byte_count": start_path.stat().st_size,
                "run_id": start_manifest.get("run_id"),
            },
            "final": {
                "sha256": sha256_bytes(_read_regular(final_path)),
                "byte_count": final_path.stat().st_size,
                "run_id": final_manifest.get("run_id"),
                "selected_task_capture_complete": True,
                "run_capture_complete": final_manifest.get("capture_complete"),
            },
        },
        "curated_source": {
            "manifest_relative_path": curated_relative.as_posix(),
            "manifest_sha256": sha256_bytes(curated_bytes),
            "validation_receipt_relative_path": str(
                validation_path.relative_to(source_base).as_posix()
            ),
            "validation_receipt_sha256": sha256_bytes(validation_bytes),
            "validation_receipt_valid": True,
            "environment_evaluation_not_projected": True,
        },
        "task": {
            "catalog_index": task["catalog_index"],
            "task_name": task["task_name"],
            "task_run_id": task["task_run_id"],
            "task_instruction": chain["task_started"]["payload"]["task_goal"],
            "task_instruction_sha256": task["task_instruction_sha256"],
            "task_parameters_projection_sha256": task["task_parameters_sha256"],
            "hidden_generator_checker_parameters_included": False,
        },
        "registry_row": {
            "file": unit.registry_file,
            "line_number": unit.line_number,
            "line_sha256": unit.line_sha256,
            "full_row_visibility": "POST_ACTION_AUDIT_ONLY",
        },
    }


def _model_config(
    *,
    row: Mapping[str, Any],
    request: Mapping[str, Any],
    model_record: Mapping[str, Any],
    start_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _require(
        canonical_sha256(model_record) == row["model_config_record_sha256"],
        "REGISTRY_BINDING_INVALID",
        "model configuration record hash changed",
        stage="MODEL_CONFIG",
    )
    expected_family = row["history_family"].lower()
    _require(
        model_record.get("model_id") == row["model_id"]
        and model_record.get("role") == row["study_role"]
        and model_record.get("history_family") == expected_family,
        "REGISTRY_BINDING_INVALID",
        "model configuration identity changed",
        stage="MODEL_CONFIG",
    )
    payload = request["payload"]
    captured = model_record.get("captured_application_request")
    _require(
        isinstance(captured, Mapping)
        and payload.get("endpoint", {}).get("origin") == captured.get("endpoint_origin")
        and payload.get("endpoint", {}).get("path") == captured.get("endpoint_path")
        and payload.get("sdk", {}).get("version") == captured.get("sdk_version"),
        "REGISTRY_BINDING_INVALID",
        "captured provider application boundary changed",
        stage="MODEL_CONFIG",
    )
    source_platform = start_manifest.get("mobile_world_snapshot")
    return {
        "manifest_sha256": MODEL_MANIFEST_SHA256,
        "record_sha256": row["model_config_record_sha256"],
        "model_id": model_record["model_id"],
        "model_repository": model_record["model_repository"],
        "model_revision": model_record["model_revision"],
        "served_model_name": model_record["served_model_name"],
        "history_family": model_record["history_family"],
        "host_component": payload.get("component"),
        "adapter": _json_clone(model_record.get("actor_adapter")),
        "parser": _json_clone(model_record.get("parser_implementation")),
        "provider_application_boundary": {
            "endpoint": _json_clone(payload.get("endpoint")),
            "sdk": _json_clone(payload.get("sdk")),
            "excluded_transport_fields": _json_clone(payload.get("excluded_transport_fields")),
            "provider_seed_present": "seed" in request["payload"].get("request_view", {}),
        },
        "source_platform": {
            "environment_image": start_manifest.get("environment_image"),
            "mobile_world_snapshot": _json_clone(source_platform),
            "python_version": start_manifest.get("python_version"),
            "apk_inventory": {
                "status": "UNAVAILABLE_FROM_CAPTURE",
                "items": None,
                "not_invented": True,
            },
        },
        "checkpoint_inventory_bound_by_manifest": True,
    }


def _curator_channels(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    current_state: Mapping[str, Any],
) -> dict[str, Any]:
    cutoff = chain["request"]["seq"]
    task_evidence = {
        "role": "task_instruction",
        "event_id": chain["task_started"]["event_id"],
        "event_seq": chain["task_started"]["seq"],
        "sha256": row["task"]["task_instruction_sha256"],
    }
    current_evidence = {
        "role": "target_pre",
        "event_id": chain["pre"]["event_id"],
        "event_seq": chain["pre"]["seq"],
        "sha256": current_state["observation_sha256"],
    }
    history_evidence = [
        {
            "role": "source_history",
            "candidate_id": target["candidate_id"],
            "event_id": chain["request"]["event_id"],
            "event_seq": chain["request"]["seq"],
            "sha256": target["record_sha256"],
        }
        for target in targets
    ]
    _require(
        all(
            evidence["event_seq"] <= cutoff
            for evidence in (task_evidence, current_evidence, *history_evidence)
        ),
        "FUTURE_EVIDENCE_LEAKAGE",
        "curator evidence exceeds request cutoff",
        stage="VISIBILITY",
    )
    return {
        "request_cutoff_event_seq": cutoff,
        "action_gold": {
            "consumer": "ACTION_GOLD_CURATOR",
            "allowed_evidence_roles": [
                "ask_user_response",
                "target_pre",
                "task_instruction",
                "tool_response",
            ],
            "evidence": [task_evidence, current_evidence],
            "history_visible": False,
            "natural_target_response_action_post_visible": False,
            "curation_phase": "G1_6_PENDING",
        },
        "transformation": {
            "consumer": "TRANSFORMATION_CURATOR",
            "allowed_evidence_roles": [
                "source_history",
                "target_pre",
                "task_instruction",
            ],
            "evidence": [task_evidence, current_evidence, *history_evidence],
            "natural_target_response_action_post_visible": False,
            "curation_phase": "G1_6_PENDING",
        },
    }


def _post_action_audit(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    stream: EventStream,
    blob_store: BlobStore,
    legacy_capsule_sha256: Any,
) -> dict[str, Any]:
    response = chain["response"]
    decision = chain["decision"]
    execution = chain["execution"]
    terminal = chain["terminal"]
    parsed_action = decision["payload"].get("parsed_action")
    _require(
        parsed_action == row["frozen_capsule"]["original_action"]["parsed_action"]
        and canonical_sha256(parsed_action)
        == row["frozen_capsule"]["original_action"]["parsed_action_sha256"],
        "ORIGINAL_ACTION_UNRESOLVED",
        "captured natural action differs from frozen reference",
        stage="POST_ACTION",
    )
    response_refs = _verified_blob_summaries(response, blob_store)
    decision_refs = _verified_blob_summaries(decision, blob_store)
    terminal_refs = _verified_blob_summaries(terminal, blob_store)
    execution_refs = _verified_blob_summaries(execution, blob_store) if execution else []
    if terminal["event_type"] == "transition_completed":
        post_observation = terminal["payload"].get("post_observation")
        result = terminal["payload"].get("execution_result")
        terminal_status = "COMPLETED"
    else:
        post_observation = None
        result = None
        terminal_status = "NOT_EXECUTED_TERMINAL_ACTION"
    prediction = decision["payload"].get("prediction_raw")
    return {
        "visibility": "POST_ACTION_AUDIT_ONLY",
        "historical_reference_only": True,
        "never_expected_replay_output": True,
        "legacy_g1_1_capsule_sha256_includes_natural_action": legacy_capsule_sha256,
        "response": {
            "event": _event_ref(response, stream),
            "payload_sha256": canonical_sha256(response["payload"]),
            "blob_refs": response_refs,
        },
        "decision": {
            "event": _event_ref(decision, stream),
            "payload_sha256": canonical_sha256(decision["payload"]),
            "prediction_sha256": sha256_bytes(prediction.encode("utf-8"))
            if isinstance(prediction, str)
            else None,
            "parsed_action": _json_clone(parsed_action),
            "parsed_action_sha256": canonical_sha256(parsed_action),
            "blob_refs": decision_refs,
        },
        "execution": {
            "status": "EXECUTED" if execution is not None else "NOT_APPLICABLE",
            "event": _event_ref(execution, stream) if execution is not None else None,
            "blob_refs": execution_refs,
        },
        "terminal": {
            "status": terminal_status,
            "event": _event_ref(terminal, stream),
            "execution_result_sha256": canonical_sha256(result) if result is not None else None,
            "post_observation_sha256": canonical_sha256(post_observation)
            if post_observation is not None
            else None,
            "blob_refs": terminal_refs,
        },
    }


def _artifact_closure(
    *,
    run_root: Path,
    blob_store: BlobStore,
    graph: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    occurrences: dict[str, dict[str, Any]] = {}

    def collect(value: Any, role: str, path: tuple[str | int, ...] = ()) -> None:
        if _looks_like_blob_ref(value):
            _require_blob_ref(value, code="BLOB_REFERENCE_INVALID")
            _verify_blob(blob_store, value)
            digest = value["digest"]
            existing = occurrences.get(digest)
            if existing is None:
                occurrences[digest] = {
                    "store_id": run_root.name,
                    "algorithm": "sha256",
                    "digest": digest,
                    "byte_length": value["byte_length"],
                    "media_type": value["media_type"],
                    "relative_path": value["relative_path"],
                    "roles": [role],
                    "source_json_paths": [_json_pointer(path)],
                }
            else:
                _require(
                    existing["byte_length"] == value["byte_length"]
                    and existing["relative_path"] == value["relative_path"],
                    "BLOB_REFERENCE_INVALID",
                    "same digest has inconsistent blob metadata",
                    stage="ARTIFACT_CLOSURE",
                )
                if role not in existing["roles"]:
                    existing["roles"].append(role)
                pointer = _json_pointer(path)
                if pointer not in existing["source_json_paths"]:
                    existing["source_json_paths"].append(pointer)
            return
        if isinstance(value, Mapping):
            for key in sorted(value):
                collect(value[key], role, (*path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, role, (*path, index))

    collect(graph, "SDK_ARTIFACT_GRAPH")
    for event in events:
        role = (
            "PRE_REQUEST_EVENT"
            if event["event_type"] in {"task_started", "step_started", "model_request"}
            else "POST_ACTION_AUDIT_EVENT"
        )
        collect(event, role)
    values = list(occurrences.values())
    for value in values:
        value["roles"].sort()
        value["source_json_paths"].sort()
    values.sort(key=lambda value: (value["digest"], value["relative_path"]))
    return values


def _projection_hashes(capsule: Mapping[str, Any]) -> dict[str, str]:
    runtime = {
        "identity": {
            key: capsule["identity"][key]
            for key in (
                "unit_kind",
                "unit_id",
                "source_key",
                "study_role",
                "model_id",
                "history_family",
                "task_run_id",
                "target_step",
                "step_id",
                "request_event_id",
                "request_id",
                "model_call_id",
            )
        },
        "model_config": capsule["model_config"],
        "exact_request": capsule["exact_request"],
        "request_regions": capsule["request_regions"],
        "current_state": capsule["current_state"],
        "treatment_surface": capsule["treatment_surface"],
        "state_restore": capsule["state_restore"],
    }
    action_gold = {
        "task": capsule["source_provenance"]["task"],
        "current_state": capsule["current_state"],
        "channel": capsule["curator_channels"]["action_gold"],
    }
    transformation = {
        "task": capsule["source_provenance"]["task"],
        "current_state": capsule["current_state"],
        "treatment_surface": capsule["treatment_surface"],
        "channel": capsule["curator_channels"]["transformation"],
    }
    cutoff = capsule["current_state"]["request_cutoff"]["event_seq"]
    _require(
        _max_event_seq(runtime) <= cutoff
        and _max_event_seq(action_gold) <= cutoff
        and _max_event_seq(transformation) <= cutoff,
        "FUTURE_EVIDENCE_LEAKAGE",
        "runtime or curator projection contains post-request event",
        stage="VISIBILITY",
    )
    _require(
        "post_action_audit" not in runtime
        and "post_action_audit" not in action_gold
        and "post_action_audit" not in transformation,
        "FIELD_VISIBILITY_INVALID",
        "post-action audit root leaked into a restricted projection",
        stage="VISIBILITY",
    )
    return {
        "runtime_renderer_sha256": canonical_sha256(runtime),
        "action_gold_curator_sha256": canonical_sha256(action_gold),
        "transformation_curator_sha256": canonical_sha256(transformation),
        "auditor_full_pre_hash_sha256": canonical_sha256(
            {key: value for key, value in capsule.items() if key != "post_action_audit"}
        ),
    }


def _require(
    condition: bool,
    code: str,
    message: str,
    *,
    stage: str = "VALIDATE",
    json_path: str = "$",
    **context: Any,
) -> None:
    if not condition:
        raise ReplayCapsuleError(
            code,
            message,
            stage=stage,
            json_path=json_path,
            **context,
        )


def _require_sha(value: Any, label: str) -> str:
    _require(
        isinstance(value, str) and _SHA_RE.fullmatch(value) is not None,
        "SCHEMA_VALIDATION_FAILED",
        f"{label} is not a lowercase SHA-256 digest",
    )
    return value


def _require_int_range(start: Any, end: Any, size: int, label: str) -> None:
    _require(
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= size,
        "TARGET_SPAN_COORDINATE_MISMATCH",
        f"{label} is outside its source container",
        stage="TARGET",
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_json_bytes(data: bytes, path: Path) -> Any:
    del path
    try:
        return json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReplayCapsuleError(
            "SCHEMA_VALIDATION_FAILED",
            "invalid JSON",
            stage="READ",
            error_type=type(error).__name__,
        ) from error


def _read_regular(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReplayCapsuleError(
            "SOURCE_REFERENCE_UNRESOLVED",
            "required file is missing or unreadable",
            stage="READ",
            error_type=type(error).__name__,
        ) from error
    _require(
        stat.S_ISREG(metadata.st_mode) and not path.is_symlink(),
        "SOURCE_REFERENCE_UNRESOLVED",
        "path is not a regular non-symlink file",
        stage="READ",
    )
    try:
        with path.open("rb") as handle:
            data = handle.read()
        after = path.stat()
    except OSError as error:
        raise ReplayCapsuleError(
            "SOURCE_HASH_MISMATCH",
            "file became unreadable while being verified",
            stage="READ",
            error_type=type(error).__name__,
        ) from error
    _require(
        (metadata.st_dev, metadata.st_ino, metadata.st_size)
        == (after.st_dev, after.st_ino, after.st_size),
        "SOURCE_HASH_MISMATCH",
        "file changed while being read",
        stage="READ",
    )
    return data


def _load_canonical_object_bytes(data: bytes, path: Path) -> dict[str, Any]:
    value = _parse_json_bytes(data, path)
    _require(
        isinstance(value, dict) and data == canonical_json_line(value),
        "SCHEMA_VALIDATION_FAILED",
        "JSON object is not canonical",
        stage="READ",
    )
    return value


def _load_pinned_object_bytes(data: bytes, path: Path) -> dict[str, Any]:
    """Parse a byte-hash-pinned JSON object without changing its formatting."""

    value = _parse_json_bytes(data, path)
    _require(
        isinstance(value, dict),
        "SCHEMA_VALIDATION_FAILED",
        "pinned JSON value is not an object",
        stage="READ",
    )
    return value


def _load_canonical_object(path: Path) -> dict[str, Any]:
    return _load_canonical_object_bytes(_read_regular(path), path)


def _load_canonical_line_bytes(data: bytes, path: Path, line_number: int) -> dict[str, Any]:
    value = _parse_json_bytes(data, path)
    _require(
        isinstance(value, dict) and data == canonical_json_line(value),
        "SCHEMA_VALIDATION_FAILED",
        "JSONL record is not a canonical newline-terminated object",
        stage="READ",
        line_number=line_number,
    )
    return value


def _load_canonical_jsonl(path: Path) -> list[dict[str, Any]]:
    data = _read_regular(path)
    records = [
        _load_canonical_line_bytes(line, path, line_number)
        for line_number, line in enumerate(data.splitlines(keepends=True), start=1)
    ]
    if not data:
        return []
    _require(
        records and b"".join(canonical_json_line(record) for record in records) == data,
        "SCHEMA_VALIDATION_FAILED",
        "JSONL file is not canonical",
        stage="READ",
    )
    return records


def _json_clone(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value))


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    _require(
        isinstance(value, str) and bool(value),
        "SOURCE_REFERENCE_UNRESOLVED",
        f"{label} path is empty",
        stage="PATH",
    )
    relative = PurePosixPath(value)
    _require(
        not relative.is_absolute()
        and relative.as_posix() == value
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in relative.parts),
        "SOURCE_REFERENCE_UNRESOLVED",
        f"{label} path is not a safe canonical relative path",
        stage="PATH",
    )
    return relative


def _safe_child(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise ReplayCapsuleError(
            "SOURCE_REFERENCE_UNRESOLVED",
            "relative path parent is missing or unreadable",
            stage="PATH",
            error_type=type(error).__name__,
        ) from error
    _require(
        _is_within(resolved_parent, root),
        "SOURCE_REFERENCE_UNRESOLVED",
        "relative path escapes its immutable root",
        stage="PATH",
    )
    return resolved_parent / candidate.name


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _valid_output_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and name not in {".", ".."}
        and (name in BASE_OUTPUT_FILE_NAMES or name.startswith(("capsule-", "artifact-")))
    )


def _validate_exact_regular_file_set(root: Path, expected: set[str]) -> None:
    _require(
        root.is_dir() and not root.is_symlink(),
        "SOURCE_REFERENCE_UNRESOLVED",
        "artifact root is not a regular directory",
        stage="FILE_SET",
        json_path=str(root),
    )
    actual: set[str] = set()
    for child in root.iterdir():
        metadata = child.lstat()
        _require(
            stat.S_ISREG(metadata.st_mode) and not child.is_symlink(),
            "SOURCE_REFERENCE_UNRESOLVED",
            "artifact root contains a non-regular child",
            stage="FILE_SET",
            json_path=str(child),
        )
        actual.add(child.name)
    _require(
        actual == expected,
        "SOURCE_REFERENCE_UNRESOLVED",
        "artifact root file set differs from its manifest",
        stage="FILE_SET",
        missing=sorted(expected - actual),
        extra=sorted(actual - expected),
    )


def _schema_validators(repo: Path) -> dict[str, Draft202012Validator]:
    names = {
        "capsule": "replay_capsule.v1_1.schema.json",
        "legacy_capsule": "replay_capsule.schema.json",
        "manifest": "capsule_manifest.v1_1.schema.json",
        "legacy_manifest": "capsule_manifest.schema.json",
        "integrity": "capsule_integrity.v1_1.schema.json",
        "legacy_integrity": "capsule_integrity.schema.json",
        "exclusion": "capsule_exclusion.schema.json",
        "visibility": "field_visibility.schema.json",
    }
    schemas: dict[str, dict[str, Any]] = {}
    store: dict[str, dict[str, Any]] = {}
    for key, name in names.items():
        path = repo / "mobileworld_audit_handoff/schemas/g1_3" / name
        schema = _load_pinned_object_bytes(_read_regular(path), path)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as error:
            raise ReplayCapsuleError(
                "SCHEMA_VALIDATION_FAILED",
                "G1.3 JSON schema fails Draft 2020-12 meta-validation",
                stage="SCHEMA",
                schema=name,
                error_type=type(error).__name__,
            ) from error
        schemas[key] = schema
        identifier = schema.get("$id")
        _require(
            isinstance(identifier, str) and bool(identifier),
            "SCHEMA_VALIDATION_FAILED",
            "G1.3 schema has no stable $id",
            stage="SCHEMA",
        )
        store[identifier] = schema
    return {
        key: Draft202012Validator(
            schema,
            resolver=RefResolver.from_schema(schema, store=store),
        )
        for key, schema in schemas.items()
    }


def _versioned_validator(
    validators: Mapping[str, Draft202012Validator],
    *,
    artifact_kind: str,
    instance: Mapping[str, Any],
) -> Draft202012Validator:
    """Select a frozen schema by its declared version without reinterpretation."""

    active_and_legacy = {
        "capsule": (CAPSULE_SCHEMA_VERSION, LEGACY_CAPSULE_SCHEMA_VERSION),
        "manifest": (MANIFEST_SCHEMA_VERSION, LEGACY_MANIFEST_SCHEMA_VERSION),
        "integrity": (INTEGRITY_SCHEMA_VERSION, LEGACY_INTEGRITY_SCHEMA_VERSION),
    }
    _require(
        artifact_kind in active_and_legacy,
        "SCHEMA_VALIDATION_FAILED",
        "unknown versioned G1.3 artifact kind",
        stage="SCHEMA",
    )
    active_version, legacy_version = active_and_legacy[artifact_kind]
    version = instance.get("schema_version")
    if version == active_version:
        return validators[artifact_kind]
    if version == legacy_version:
        return validators[f"legacy_{artifact_kind}"]
    raise ReplayCapsuleError(
        "SCHEMA_VALIDATION_FAILED",
        f"unsupported {artifact_kind} schema version",
        stage="SCHEMA",
        json_path="/schema_version",
    )


def _artifact_schema_generation(artifacts: Mapping[str, Any]) -> str:
    """Require one coherent legacy-v1 or active-v1.1 publication generation."""

    manifest = artifacts["manifest"]
    integrity = artifacts["integrity"]
    capsules = artifacts["capsules"]
    _require(
        isinstance(manifest, Mapping)
        and isinstance(integrity, Mapping)
        and isinstance(capsules, Sequence)
        and not isinstance(capsules, (str, bytes, bytearray)),
        "SCHEMA_VALIDATION_FAILED",
        "G1.3 artifact generation inputs have invalid container types",
        stage="SCHEMA",
    )
    manifest_version = manifest.get("schema_version")
    if manifest_version == MANIFEST_SCHEMA_VERSION:
        expected_integrity = INTEGRITY_SCHEMA_VERSION
        expected_capsule = CAPSULE_SCHEMA_VERSION
        generation = "ACTIVE_V1_1"
    elif manifest_version == LEGACY_MANIFEST_SCHEMA_VERSION:
        expected_integrity = LEGACY_INTEGRITY_SCHEMA_VERSION
        expected_capsule = LEGACY_CAPSULE_SCHEMA_VERSION
        generation = "LEGACY_V1"
    else:
        raise ReplayCapsuleError(
            "SCHEMA_VALIDATION_FAILED",
            "unsupported manifest schema version",
            stage="SCHEMA",
            json_path="/schema_version",
        )
    _require(
        integrity.get("schema_version") == expected_integrity,
        "SCHEMA_VALIDATION_FAILED",
        "manifest and integrity schema generations differ",
        stage="SCHEMA",
        json_path="/schema_version",
    )
    for index, envelope in enumerate(capsules):
        _require(
            isinstance(envelope, Mapping) and envelope.get("schema_version") == expected_capsule,
            "SCHEMA_VALIDATION_FAILED",
            "manifest and capsule schema generations differ",
            stage="SCHEMA",
            json_path=f"/capsules/{index}/schema_version",
        )
    return generation


def _require_exact_false(value: Any, *, json_path: str) -> None:
    _require(
        type(value) is bool and value is False,
        "SCHEMA_VALIDATION_FAILED",
        "G1.3 authorization guard must be the boolean false",
        stage="SCHEMA",
        json_path=json_path,
    )


def _validate_active_authorization_guards(artifacts: Mapping[str, Any]) -> None:
    """Independently enforce v1.1 authorization guards beyond JSON Schema."""

    if _artifact_schema_generation(artifacts) == "LEGACY_V1":
        return
    for index, envelope in enumerate(artifacts["capsules"]):
        body = envelope.get("capsule")
        safety = body.get("safety") if isinstance(body, Mapping) else None
        if not isinstance(safety, Mapping):
            safety = {}
        for field in (
            "execution_ready",
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
        ):
            _require_exact_false(
                safety.get(field),
                json_path=f"/capsules/{index}/capsule/safety/{field}",
            )
    manifest = artifacts["manifest"]
    integrity = artifacts["integrity"]
    readiness = manifest.get("readiness")
    if not isinstance(readiness, Mapping):
        readiness = {}
    for field in (
        "execution_ready",
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
    ):
        _require_exact_false(
            readiness.get(field),
            json_path=f"/manifest/readiness/{field}",
        )
    for container_name, safety in (
        ("manifest", manifest.get("safety")),
        ("integrity", integrity.get("safety")),
    ):
        if not isinstance(safety, Mapping):
            safety = {}
        for field in (
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
        ):
            _require_exact_false(
                safety.get(field),
                json_path=f"/{container_name}/safety/{field}",
            )
    integrity_safety = integrity.get("safety")
    if not isinstance(integrity_safety, Mapping):
        integrity_safety = {}
    _require_exact_false(
        integrity_safety.get("execution_ready"),
        json_path="/integrity/safety/execution_ready",
    )


def _require_active_artifact_generation(artifacts: Mapping[str, Any]) -> None:
    _require(
        all(key in artifacts for key in ("manifest", "integrity", "capsules")),
        "SCHEMA_VALIDATION_FAILED",
        "formal artifact set is missing a versioned root",
        stage="SCHEMA",
        json_path="/",
    )
    _require(
        _artifact_schema_generation(artifacts) == "ACTIVE_V1_1",
        "SCHEMA_VALIDATION_FAILED",
        "formal writes require the active amended G1.3 artifact generation",
        stage="SCHEMA",
        json_path="/manifest/schema_version",
    )


def _validate_instance(validator: Draft202012Validator, instance: Any, *, label: str) -> None:
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (list(error.absolute_path), error.validator or ""),
    )
    if not errors:
        return
    first = errors[0]
    pointer = _json_pointer(list(first.absolute_path))
    raise ReplayCapsuleError(
        "SCHEMA_VALIDATION_FAILED",
        f"{label} does not satisfy its frozen G1.3 schema",
        stage="SCHEMA",
        json_path=pointer or None,
        validator=first.validator,
        error_count=len(errors),
    )


def _manifest_payload_refs(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    files = manifest["files"]
    fixed = {
        "field_visibility.json": files["field_visibility_policy"],
        "capsule_index.jsonl": files["capsule_index"],
        "capsule_exclusions.jsonl": files["exclusion_ledger"],
        "capsule_integrity.json": files["integrity_report"],
    }
    references: dict[str, Mapping[str, Any]] = {}
    for name, reference in fixed.items():
        _require(
            reference["relative_path"] == name,
            "SCHEMA_VALIDATION_FAILED",
            "manifest fixed file reference uses the wrong path",
            stage="SCHEMA",
            json_path=f"/files/{name}",
        )
        references[name] = reference
    for reference in files["capsule_files"]:
        name = reference["relative_path"]
        _require(
            name not in references,
            "SCHEMA_VALIDATION_FAILED",
            "manifest payload file reference is duplicated",
            stage="SCHEMA",
            json_path=f"/files/capsule_files/{name}",
        )
        references[name] = reference
    for reference in files["artifact_files"]:
        name = reference["relative_path"]
        _require(
            name not in references,
            "SCHEMA_VALIDATION_FAILED",
            "manifest artifact file reference is duplicated",
            stage="SCHEMA",
            json_path=f"/files/artifact_files/{name}",
        )
        references[name] = reference
    return references


def _validate_runtime_replay_binding(
    capsule: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    semantic_request: Mapping[str, Any],
) -> None:
    binding = capsule["runtime"]["non_history_envelope"]["replay_binding"]
    parser_reference = binding["parser"]["implementation_ref"]
    _require(
        binding == _runtime_replay_binding(capsule["source_provenance"], parser_reference),
        "REGISTRY_BINDING_INVALID",
        "runtime replay binding differs from frozen source provenance",
        stage="MODEL_CONFIG",
    )
    parser_data = payloads.get(parser_reference["relative_path"])
    _require(
        parser_reference["store_id"] == "G1_3_PUBLICATION" and isinstance(parser_data, bytes),
        "SOURCE_REFERENCE_UNRESOLVED",
        "runtime parser binding artifact is unavailable",
        stage="MODEL_CONFIG",
    )
    assert isinstance(parser_data, bytes)
    _verify_file_summary(parser_data, parser_reference, parser_reference["relative_path"])
    parser_value = _parse_json_bytes(parser_data, Path(parser_reference["relative_path"]))
    _require(
        isinstance(parser_value, Mapping)
        and parser_data == canonical_json_bytes(parser_value)
        and canonical_sha256(parser_value) == binding["parser"]["implementation_sha256"],
        "REGISTRY_BINDING_INVALID",
        "runtime parser binding artifact differs from the pinned parser identity",
        stage="MODEL_CONFIG",
    )
    provider = binding["provider"]
    decoding_reference = provider["decoding_configuration_ref"]
    decoding_data = payloads.get(decoding_reference["relative_path"])
    _require(
        decoding_reference["store_id"] == "G1_3_PUBLICATION" and isinstance(decoding_data, bytes),
        "SOURCE_REFERENCE_UNRESOLVED",
        "runtime decoding-configuration artifact is unavailable",
        stage="MODEL_CONFIG",
    )
    assert isinstance(decoding_data, bytes)
    _verify_file_summary(decoding_data, decoding_reference, decoding_reference["relative_path"])
    decoding_value = _parse_json_bytes(decoding_data, Path(decoding_reference["relative_path"]))
    expected_decoding = {
        key: _json_clone(value)
        for key, value in sorted(semantic_request.items())
        if key != "messages"
    }
    decoding_sha256 = canonical_sha256(expected_decoding)
    _require(
        isinstance(decoding_value, Mapping)
        and decoding_data == canonical_json_bytes(decoding_value)
        and decoding_value == expected_decoding
        and decoding_reference["sha256"] == decoding_sha256
        and provider["decoding_configuration_sha256"] == decoding_sha256
        and capsule["runtime"]["non_history_envelope"]["provider_envelope_sha256"]
        == decoding_sha256,
        "REQUEST_HASH_MISMATCH",
        "runtime decoding configuration differs from the exact semantic request envelope",
        stage="REQUEST",
    )


def _validate_artifacts_against_schemas(repo: Path, artifacts: Mapping[str, Any]) -> None:
    validators = _schema_validators(repo)
    capsules = artifacts["capsules"]
    exclusions = artifacts["exclusions"]
    visibility = artifacts["visibility"]
    integrity = artifacts["integrity"]
    manifest = artifacts["manifest"]
    payloads = artifacts["file_payloads"]
    generation = _artifact_schema_generation(artifacts)
    _validate_active_authorization_guards(artifacts)
    _validate_instance(validators["visibility"], visibility, label="visibility policy")
    _validate_instance(
        _versioned_validator(
            validators,
            artifact_kind="integrity",
            instance=integrity,
        ),
        integrity,
        label="integrity report",
    )
    _validate_instance(
        _versioned_validator(
            validators,
            artifact_kind="manifest",
            instance=manifest,
        ),
        manifest,
        label="publication manifest",
    )
    for record in capsules:
        _validate_instance(
            _versioned_validator(
                validators,
                artifact_kind="capsule",
                instance=record,
            ),
            record,
            label="replay capsule",
        )
        _require(
            record["capsule_body_sha256"] == canonical_sha256(record["capsule"]),
            "CAPSULE_HASH_MISMATCH",
            "capsule body hash is invalid",
            stage="SCHEMA",
        )
        capsule = record["capsule"]
        semantic_ref = capsule["runtime"]["model_visible"]["semantic_request"][
            "canonical_semantic_request_ref"
        ]
        semantic_data = payloads.get(semantic_ref["relative_path"])
        _require(
            semantic_ref["store_id"] == "G1_3_PUBLICATION" and isinstance(semantic_data, bytes),
            "SOURCE_REFERENCE_UNRESOLVED",
            "capsule canonical semantic-request artifact is unavailable",
            stage="SCHEMA",
        )
        _verify_file_summary(semantic_data, semantic_ref, semantic_ref["relative_path"])
        semantic_request = _parse_json_bytes(semantic_data, Path(semantic_ref["relative_path"]))
        _require(
            isinstance(semantic_request, Mapping)
            and semantic_data == canonical_json_bytes(semantic_request),
            "REQUEST_HASH_MISMATCH",
            "canonical semantic-request artifact bytes are not exact canonical JSON",
            stage="SCHEMA",
        )
        model_visible = capsule["runtime"]["model_visible"]
        region_partition = model_visible["region_partition"]
        _validate_semantic_request_partition(semantic_request, region_partition)
        _require(
            model_visible["partition_sha256"] == canonical_sha256(region_partition)
            and model_visible["non_history_projection_sha256"]
            == canonical_sha256(_non_history_projection(semantic_request, region_partition)),
            "REQUEST_HASH_MISMATCH",
            "request partition or non-history projection hash is not independently reproducible",
            stage="SCHEMA",
        )
        _validate_targets_inside_history(
            capsule["runtime"]["treatment_surface"]["target_exposures"],
            region_partition,
        )
        _validate_runtime_replay_binding(capsule, payloads, semantic_request)
        _validate_visibility_boundary(capsule)
        binding = capsule["integrity_binding"]
        expected_sections = {
            "source_provenance": canonical_sha256(capsule["source_provenance"]),
            "model_visible": canonical_sha256(capsule["runtime"]["model_visible"]),
            "non_history_envelope": canonical_sha256(capsule["runtime"]["non_history_envelope"]),
            "treatment_surface": canonical_sha256(capsule["runtime"]["treatment_surface"]),
            "curator_only": canonical_sha256(capsule["curator_only"]),
            "post_action_audit": canonical_sha256(capsule["post_action_audit"]),
            "field_visibility": canonical_sha256(capsule["field_visibility"]),
            "artifact_closure": canonical_sha256(capsule["artifact_closure"]),
        }
        _require(
            binding["source_closure_sha256"] == _source_closure_sha256(capsule["artifact_closure"])
            and binding["runtime_projection_sha256"] == canonical_sha256(capsule["runtime"])
            and binding["section_sha256s"] == expected_sections,
            "CAPSULE_HASH_MISMATCH",
            "capsule integrity binding is not independently reproducible",
            stage="SCHEMA",
        )
        _validate_artifact_closure(capsule)
    for record in exclusions:
        _validate_instance(validators["exclusion"], record, label="capsule exclusion")
    capsule_by_unit = {record["capsule"]["unit"]["unit_id"]: record for record in capsules}
    _require(
        len(capsules) + len(exclusions) == TARGET_POPULATION
        and len(manifest["units"]) == TARGET_POPULATION
        and len(capsule_by_unit) == len(capsules)
        and list(manifest["units"]) == sorted(manifest["units"], key=_manifest_unit_sort_key),
        "REGISTRY_BINDING_INVALID",
        "artifact population or canonical unit ordering is invalid",
        stage="SCHEMA",
    )
    _require(
        manifest["capsule_set_sha256"]
        == _capsule_set_sha256(manifest["units"])
        == integrity["capsule_set_sha256"],
        "CAPSULE_HASH_MISMATCH",
        "capsule-set digest cross-binding is invalid",
        stage="SCHEMA",
    )
    _require(
        manifest["publication_phase"]
        == integrity["report_phase"]
        == manifest["finalization"]["integrity_report_phase"]
        and manifest["finalization"]["double_build_status"] == integrity["double_build"]["status"],
        "CAPSULE_HASH_MISMATCH",
        "manifest and integrity finalization phases are not cross-bound",
        stage="SCHEMA",
    )
    actual_capsuled_count = len(capsules)
    actual_excluded_count = len(exclusions)
    _require(
        manifest["counts"]["capsuled_count"] == actual_capsuled_count
        and manifest["counts"]["excluded_count"] == actual_excluded_count
        and integrity["counts"]["capsuled_count"] == actual_capsuled_count
        and integrity["counts"]["excluded_count"] == actual_excluded_count
        and integrity["counts"]["unit_receipt_count"] == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "manifest or integrity counts differ from the materialized dispositions",
        stage="SCHEMA",
    )
    index_bytes = payloads.get("capsule_index.jsonl")
    _require(
        isinstance(index_bytes, bytes)
        and index_bytes == b"".join(canonical_json_line(record) for record in manifest["units"]),
        "CAPSULE_HASH_MISMATCH",
        "capsule index bytes differ from manifest unit entries",
        stage="SCHEMA",
    )
    exclusion_bytes = payloads.get("capsule_exclusions.jsonl")
    _require(
        isinstance(exclusion_bytes, bytes)
        and exclusion_bytes == b"".join(canonical_json_line(record) for record in exclusions),
        "CAPSULE_HASH_MISMATCH",
        "exclusion ledger bytes differ from exclusions",
        stage="SCHEMA",
    )
    receipts = integrity["unit_receipts"]
    _require(
        [record["unit_id"] for record in receipts]
        == [record["unit_id"] for record in manifest["units"]]
        and len({record["unit_id"] for record in receipts}) == TARGET_POPULATION,
        "REGISTRY_BINDING_INVALID",
        "integrity receipts do not have the exact manifest unit order",
        stage="SCHEMA",
    )
    exclusion_ref = _file_summary(payloads["capsule_exclusions.jsonl"], "capsule_exclusions.jsonl")
    exclusion_by_unit = {record["unit_id"]: record for record in exclusions}
    for unit, receipt in zip(manifest["units"], receipts, strict=True):
        expected_receipt_binding = {
            "unit_kind": unit["unit_kind"],
            "unit_id": unit["unit_id"],
            "source_key": unit["source_key"],
            "history_family": unit["history_family"],
            "disposition": unit["disposition"],
            "capsule_ref": unit["capsule_ref"],
            "capsule_body_sha256": unit["capsule_body_sha256"],
            "exclusion_ref": (exclusion_ref if unit["disposition"] == "EXCLUDED" else None),
            "exclusion_sha256": unit["exclusion_record_sha256"],
            "checks": _unit_checks(unit, exclusion_by_unit),
            "valid_capsule": unit["disposition"] == "CAPSULED",
        }
        _require(
            receipt == expected_receipt_binding,
            "CAPSULE_HASH_MISMATCH",
            "integrity unit receipt differs from its manifest disposition",
            stage="SCHEMA",
        )
        if unit["disposition"] == "CAPSULED":
            reference = unit["capsule_ref"]
            data = payloads.get(reference["relative_path"])
            _require(
                isinstance(data, bytes),
                "SOURCE_REFERENCE_UNRESOLVED",
                "manifest capsule file is missing",
                stage="SCHEMA",
            )
            _verify_file_summary(data, reference, reference["relative_path"])
            envelope = _load_canonical_object_bytes(data, Path(reference["relative_path"]))
            capsule_unit = envelope["capsule"]["unit"]
            _require(
                reference["relative_path"] == f"capsule-{unit['unit_id']}.json"
                and envelope == capsule_by_unit.get(unit["unit_id"])
                and envelope["capsule_body_sha256"] == unit["capsule_body_sha256"]
                and capsule_unit["unit_kind"] == unit["unit_kind"]
                and capsule_unit["unit_id"] == unit["unit_id"]
                and capsule_unit["source_key"] == unit["source_key"]
                and capsule_unit["model_id"] == unit["model_id"]
                and capsule_unit["history_family"] == unit["history_family"]
                and _manifest_registry_record_ref(capsule_unit["registry_record_ref"])
                == unit["registry_record_ref"],
                "REGISTRY_BINDING_INVALID",
                "manifest unit does not resolve to its exact replay capsule",
                stage="SCHEMA",
            )
        else:
            index = unit["exclusion_record_index"]
            _require(
                isinstance(index, int)
                and 0 <= index < len(exclusions)
                and exclusions[index]["unit_id"] == unit["unit_id"]
                and sha256_bytes(canonical_json_line(exclusions[index]))
                == unit["exclusion_record_sha256"],
                "CAPSULE_HASH_MISMATCH",
                "manifest exclusion row binding is invalid",
                stage="SCHEMA",
            )
    reachable_artifacts: dict[str, Mapping[str, Any]] = {}
    for record in capsules:
        for entry in record["capsule"]["artifact_closure"]["entries"]:
            reference = entry["reference"]
            if reference["store_id"] != "G1_3_PUBLICATION":
                continue
            name = reference["relative_path"]
            previous = reachable_artifacts.get(name)
            _require(
                previous is None or previous == reference,
                "CAPSULE_HASH_MISMATCH",
                "one derived artifact path has conflicting capsule references",
                stage="SCHEMA",
            )
            reachable_artifacts[name] = reference
    actual_artifact_names = {name for name in payloads if name.startswith("artifact-")}
    listed_capsule_names = {
        record["relative_path"] for record in manifest["files"]["capsule_files"]
    }
    expected_capsule_names = {
        record["capsule_ref"]["relative_path"]
        for record in manifest["units"]
        if record["disposition"] == "CAPSULED"
    }
    listed_artifact_names = {
        record["relative_path"] for record in manifest["files"]["artifact_files"]
    }
    _require(
        listed_capsule_names == expected_capsule_names
        and set(reachable_artifacts) == actual_artifact_names == listed_artifact_names,
        "SOURCE_REFERENCE_UNRESOLVED",
        "manifest payload classes differ from the exact capsule-reachable closure",
        stage="SCHEMA",
    )
    for name, reference in reachable_artifacts.items():
        _verify_file_summary(payloads[name], reference, name)
    references = _manifest_payload_refs(manifest)
    _require(
        set(references) == set(payloads) - {"capsule_manifest.json"},
        "SCHEMA_VALIDATION_FAILED",
        "manifest does not enumerate the exact non-manifest payload set",
        stage="SCHEMA",
    )
    for name, reference in references.items():
        _verify_file_summary(payloads[name], reference, name)
    _require(
        manifest["files"]["payload_file_count_excluding_manifest"] == len(references)
        and manifest["files"]["final_root_file_count"] == len(references) + 1
        and manifest["files"]["payload_file_set_aggregate_sha256"]
        == _file_set_aggregate({name: payloads[name] for name in references}),
        "CAPSULE_HASH_MISMATCH",
        "manifest file-set counts or aggregate are invalid",
        stage="SCHEMA",
    )
    _require(
        integrity["source_immutability"]["pre_build_source_file_set_sha256"]
        == integrity["source_immutability"]["post_build_source_file_set_sha256"],
        "SOURCE_HASH_MISMATCH",
        "source immutability receipt differs before and after build",
        stage="SCHEMA",
    )
    if integrity["double_build"]["status"] == "PASSED":
        core_sha256 = _file_set_aggregate(_core_file_payloads(payloads))
        _require(
            integrity["double_build"]["first_core_file_set_sha256"] == core_sha256
            and integrity["double_build"]["second_core_file_set_sha256"] == core_sha256,
            "NONDETERMINISTIC_BUILD",
            "formal double-build receipt is not bound to this core file set",
            stage="DETERMINISM",
        )
    if generation == "ACTIVE_V1_1":
        current_contract_files = [
            {
                "relative_path": record["path"],
                "sha256": record["sha256"],
                "byte_count": record["byte_count"],
            }
            for record in _contract_file_summaries(repo)
        ]
        _require(
            manifest["builder_contract"]["contract_files"] == current_contract_files,
            "SOURCE_HASH_MISMATCH",
            "manifest builder contract no longer matches repository bytes",
            stage="SCHEMA",
        )
    if "capsule_manifest.json" in payloads:
        _require(
            payloads["capsule_manifest.json"] == canonical_json_line(manifest),
            "CAPSULE_HASH_MISMATCH",
            "manifest bytes are not canonical or do not match the object",
            stage="SCHEMA",
        )


def _validate_publication_file_set(root: Path, repo: Path) -> dict[str, bytes]:
    """Validate immutable on-disk shape and return exact file bytes."""

    initial: dict[str, bytes] = {}
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ReplayCapsuleError(
            "SOURCE_REFERENCE_UNRESOLVED",
            "publication directory cannot be opened safely",
            stage="SCHEMA",
            error_type=type(error).__name__,
        ) from error
    try:
        metadata = os.fstat(directory_fd)
        _require(
            stat.S_ISDIR(metadata.st_mode) and metadata.st_mode & 0o222 == 0,
            "SOURCE_REFERENCE_UNRESOLVED",
            "publication root is not an immutable real directory",
            stage="SCHEMA",
        )
        names = sorted(os.listdir(directory_fd))
        for name in names:
            _require(
                _valid_output_name(name),
                "SOURCE_REFERENCE_UNRESOLVED",
                "publication contains an invalid filename",
                stage="SCHEMA",
            )
            item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(descriptor)
                _require(
                    stat.S_ISREG(item.st_mode)
                    and stat.S_ISREG(before.st_mode)
                    and (item.st_dev, item.st_ino) == (before.st_dev, before.st_ino)
                    and before.st_mode & 0o222 == 0
                    and before.st_nlink == 1,
                    "SOURCE_REFERENCE_UNRESOLVED",
                    "publication contains a mutable or non-regular child",
                    stage="SCHEMA",
                )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
                after = os.fstat(descriptor)
                _require(
                    (before.st_dev, before.st_ino, before.st_size)
                    == (after.st_dev, after.st_ino, after.st_size)
                    and len(data) == after.st_size,
                    "SOURCE_HASH_MISMATCH",
                    "publication child changed while being read",
                    stage="SCHEMA",
                )
                initial[name] = data
            finally:
                os.close(descriptor)
        final_metadata = os.fstat(directory_fd)
        _require(
            (final_metadata.st_dev, final_metadata.st_ino) == (metadata.st_dev, metadata.st_ino)
            and final_metadata.st_mode & 0o222 == 0
            and sorted(os.listdir(directory_fd)) == names,
            "SOURCE_REFERENCE_UNRESOLVED",
            "publication directory changed while being read",
            stage="SCHEMA",
        )
    except OSError as error:
        raise ReplayCapsuleError(
            "SOURCE_REFERENCE_UNRESOLVED",
            "publication directory changed or became unreadable",
            stage="SCHEMA",
            error_type=type(error).__name__,
        ) from error
    finally:
        os.close(directory_fd)
    _require(
        "capsule_manifest.json" in initial,
        "SOURCE_REFERENCE_UNRESOLVED",
        "publication manifest is missing",
        stage="SCHEMA",
    )
    manifest = _load_canonical_object_bytes(
        initial["capsule_manifest.json"], Path("capsule_manifest.json")
    )
    validators = _schema_validators(repo)
    _validate_instance(
        _versioned_validator(
            validators,
            artifact_kind="manifest",
            instance=manifest,
        ),
        manifest,
        label="publication manifest",
    )
    references = _manifest_payload_refs(manifest)
    expected = set(references) | {"capsule_manifest.json"}
    _require(
        set(initial) == expected,
        "SOURCE_REFERENCE_UNRESOLVED",
        "publication root differs from the exact manifest file set",
        stage="SCHEMA",
    )
    for name, reference in references.items():
        _verify_file_summary(initial[name], reference, name)
    _require(
        len(initial) == manifest["files"]["final_root_file_count"]
        and len(references) == manifest["files"]["payload_file_count_excluding_manifest"]
        and _file_set_aggregate({name: initial[name] for name in references})
        == manifest["files"]["payload_file_set_aggregate_sha256"],
        "CAPSULE_HASH_MISMATCH",
        "publication file-set aggregate is invalid",
        stage="SCHEMA",
    )
    return initial


def _file_summary(data: bytes, relative_path: str) -> dict[str, Any]:
    if relative_path.endswith(".jsonl"):
        media_type = "application/x-ndjson"
    elif relative_path.endswith(".json"):
        media_type = "application/json"
    else:
        media_type = "application/octet-stream"
    return {
        "relative_path": relative_path,
        "sha256": sha256_bytes(data),
        "byte_count": len(data),
        "media_type": media_type,
    }


def _verify_file_summary(data: bytes, summary: Any, name: str) -> None:
    _require(
        isinstance(summary, Mapping)
        and summary.get("relative_path", name) == name
        and summary.get("sha256") == sha256_bytes(data)
        and summary.get("byte_count") == len(data),
        "CAPSULE_HASH_MISMATCH",
        "file differs from its manifest summary",
        stage="FILE_SET",
        json_path=f"/{name}",
    )


def _repo_file_ref(repo: Path, relative_path: str) -> dict[str, Any]:
    relative = _safe_relative(relative_path, "repository file")
    data = _read_regular(_safe_child(repo, relative))
    return _file_summary(data, relative.as_posix())


def _contract_file_summaries(repo: Path) -> list[dict[str, Any]]:
    records = []
    for relative_path in CONTRACT_RELATIVE_PATHS:
        summary = _repo_file_ref(repo, relative_path)
        records.append(
            {
                "path": summary["relative_path"],
                "sha256": summary["sha256"],
                "byte_count": summary["byte_count"],
            }
        )
    return records


def _require_blob_ref(value: Any, *, code: str) -> None:
    _require(
        isinstance(value, Mapping)
        and set(value) == _BLOB_KEYS
        and value.get("algorithm") == "sha256"
        and isinstance(value.get("digest"), str)
        and _SHA_RE.fullmatch(value["digest"]) is not None
        and isinstance(value.get("byte_length"), int)
        and not isinstance(value.get("byte_length"), bool)
        and value["byte_length"] >= 0
        and isinstance(value.get("media_type"), str)
        and bool(value["media_type"])
        and isinstance(value.get("relative_path"), str),
        code,
        "blob reference has an invalid shape",
        stage="BLOB",
    )
    try:
        relative = _safe_relative(value["relative_path"], "blob")
    except ReplayCapsuleError as error:
        raise ReplayCapsuleError(
            code,
            "blob relative_path is not a safe canonical path",
            stage="BLOB",
        ) from error
    expected = PurePosixPath("blobs", "sha256", value["digest"][:2], value["digest"])
    _require(
        relative == expected,
        code,
        "blob relative path does not match its digest",
        stage="BLOB",
    )


def _open_blob_descriptor(
    blob_store: BlobStore, value: Mapping[str, Any]
) -> tuple[int, os.stat_result]:
    """Open a blob through dir-fd traversal without following any path component."""

    directory_descriptors: list[int] = []
    blob_descriptor: int | None = None
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            current = os.open(Path(blob_store.root), flags)
        except OSError as error:
            raise ReplayCapsuleError(
                "BLOB_REFERENCE_INVALID",
                "blob-store root cannot be opened as a real directory",
                stage="BLOB",
                error_type=type(error).__name__,
            ) from error
        directory_descriptors.append(current)
        parts = PurePosixPath(value["relative_path"]).parts
        for component in parts[:-1]:
            try:
                current = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as error:
                raise ReplayCapsuleError(
                    "BLOB_MISSING",
                    "referenced content-addressed blob directory is absent",
                    stage="BLOB",
                ) from error
            except OSError as error:
                raise ReplayCapsuleError(
                    "BLOB_REFERENCE_INVALID",
                    "blob path contains an unreadable or non-directory component",
                    stage="BLOB",
                    error_type=type(error).__name__,
                ) from error
            metadata = os.fstat(current)
            _require(
                stat.S_ISDIR(metadata.st_mode),
                "BLOB_REFERENCE_INVALID",
                "blob path contains a non-directory component",
                stage="BLOB",
            )
            directory_descriptors.append(current)
        leaf = parts[-1]
        try:
            path_metadata = os.stat(leaf, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError as error:
            raise ReplayCapsuleError(
                "BLOB_MISSING",
                "referenced content-addressed blob is absent",
                stage="BLOB",
            ) from error
        except OSError as error:
            raise ReplayCapsuleError(
                "BLOB_REFERENCE_INVALID",
                "referenced blob path is unreadable",
                stage="BLOB",
                error_type=type(error).__name__,
            ) from error
        _require(
            stat.S_ISREG(path_metadata.st_mode) and not stat.S_ISLNK(path_metadata.st_mode),
            "BLOB_REFERENCE_INVALID",
            "referenced blob is not a regular non-symlink file",
            stage="BLOB",
        )
        try:
            blob_descriptor = os.open(
                leaf,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
        except FileNotFoundError as error:
            raise ReplayCapsuleError(
                "BLOB_HASH_MISMATCH",
                "referenced blob disappeared during verification",
                stage="BLOB",
            ) from error
        except OSError as error:
            raise ReplayCapsuleError(
                "BLOB_REFERENCE_INVALID",
                "referenced blob cannot be opened safely",
                stage="BLOB",
                error_type=type(error).__name__,
            ) from error
        before = os.fstat(blob_descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and (before.st_dev, before.st_ino) == (path_metadata.st_dev, path_metadata.st_ino),
            "BLOB_HASH_MISMATCH",
            "referenced blob changed before verification",
            stage="BLOB",
        )
        result = blob_descriptor
        blob_descriptor = None
        return result, before
    finally:
        if blob_descriptor is not None:
            os.close(blob_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _verified_blob_bytes(blob_store: BlobStore, value: Any) -> bytes:
    _require_blob_ref(value, code="BLOB_REFERENCE_INVALID")
    descriptor, before = _open_blob_descriptor(blob_store, value)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ReplayCapsuleError(
            "BLOB_HASH_MISMATCH",
            "referenced blob became unreadable during verification",
            stage="BLOB",
            error_type=type(error).__name__,
        ) from error
    finally:
        os.close(descriptor)
    _require(
        (before.st_dev, before.st_ino, before.st_size)
        == (after.st_dev, after.st_ino, after.st_size)
        and len(data) == value["byte_length"] == after.st_size
        and sha256_bytes(data) == value["digest"],
        "BLOB_HASH_MISMATCH",
        "referenced blob bytes do not match their content address",
        stage="BLOB",
    )
    return data


def _verify_blob(blob_store: BlobStore, value: Any) -> None:
    _verified_blob_bytes(blob_store, value)


def _read_verified_blob(blob_store: BlobStore, value: Any) -> bytes:
    return _verified_blob_bytes(blob_store, value)


def _looks_like_blob_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and {
        "algorithm",
        "digest",
        "byte_length",
        "media_type",
        "relative_path",
    } <= set(value)


def _event(stream: EventStream, event_id: Any, event_type: str) -> dict[str, Any]:
    _require(
        isinstance(event_id, str) and bool(event_id),
        "RAW_EVENT_CHAIN_INVALID",
        "event reference is empty",
        stage="CHAIN",
    )
    value = stream.event_by_id.get(event_id)
    _require(
        isinstance(value, dict) and value.get("event_type") == event_type,
        "RAW_EVENT_CHAIN_INVALID",
        "event reference does not resolve to the required type",
        stage="CHAIN",
        event_id=event_id,
        expected_event_type=event_type,
    )
    return value


def _event_ref(event: Mapping[str, Any], stream: EventStream | None) -> dict[str, Any]:
    _require(
        stream is not None and event.get("event_id") in stream.line_sha256_by_id,
        "RAW_EVENT_CHAIN_INVALID",
        "event line identity is unavailable",
        stage="CHAIN",
    )
    return {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "seq": event["seq"],
        "monotonic_ns": event["monotonic_ns"],
        "wall_time": event["wall_time"],
        "event_line_sha256": stream.line_sha256_by_id[event["event_id"]],
        "task_stream_sha256": stream.sha256,
        "event_schema_version": event["schema_version"],
    }


def _pre_cutoff_event_ref(event: Mapping[str, Any], stream: EventStream) -> dict[str, Any]:
    """Bind one pre-cutoff line without aliasing its future-bearing full stream."""

    reference = _event_ref(event, stream)
    reference.pop("task_stream_sha256")
    return reference


def _get_path(value: Any, path: Sequence[str | int]) -> Any:
    current = value
    for part in path:
        if isinstance(part, int):
            _require(
                isinstance(current, list) and 0 <= part < len(current),
                "REQUEST_PARTITION_INCOMPLETE",
                "request path list index does not resolve",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
        else:
            _require(
                isinstance(current, Mapping) and part in current,
                "REQUEST_PARTITION_INCOMPLETE",
                "request path key does not resolve",
                stage="REQUEST",
                json_path=_json_pointer(path),
            )
        current = current[part]
    return current


def _json_pointer(path: Sequence[str | int]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _dot_path(path: Sequence[str | int]) -> str:
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += ("." if result else "") + part
    return result


def _typed_path_sort_key(path: Sequence[str | int]) -> tuple[tuple[int, Any], ...]:
    return tuple((1, part) if isinstance(part, int) else (0, part) for part in path)


def _semantic_record_path(value: Any, history_family: str) -> tuple[str | int, ...]:
    _require(
        isinstance(value, str),
        "TARGET_SPAN_COORDINATE_MISMATCH",
        "registry request path is not text",
        stage="TARGET",
    )
    matcher = (
        _QWEN_RECORD_PATH_RE.fullmatch(value)
        if history_family == "FLAT_PROGRESS"
        else _RAW_RECORD_PATH_RE.fullmatch(value)
    )
    _require(
        matcher is not None,
        "TARGET_SPAN_COORDINATE_MISMATCH",
        "registry request path does not match its frozen history family",
        stage="TARGET",
    )
    message = int(matcher.group("message"))
    if history_family == "FLAT_PROGRESS":
        return ("messages", message, "content", int(matcher.group("block")), "text")
    return ("messages", message, "content")


def _source_span(path: Sequence[str | int], source: str, start: int, end: int) -> dict[str, Any]:
    exact = source[start:end]
    return {
        "container_path": list(path),
        "char_start": start,
        "char_end": end,
        "utf8_byte_start": len(source[:start].encode("utf-8")),
        "utf8_byte_end": len(source[:end].encode("utf-8")),
        "exact_text": exact,
        "span_sha256": sha256_bytes(exact.encode("utf-8")),
    }


def _whole_value_binding(
    path: Sequence[str | int],
    value: Any,
    *,
    role: str,
    visibility_class: str,
) -> dict[str, Any]:
    return {
        "binding_kind": "WHOLE_VALUE",
        "path": list(path),
        "value_sha256": canonical_sha256(value),
        "text_slice": None,
        "artifact_ref": None,
        "semantic_role": role,
        "visibility_class": visibility_class,
    }


def _text_binding(
    path: Sequence[str | int],
    source: str,
    start: int,
    end: int,
    *,
    role: str,
    visibility_class: str,
) -> dict[str, Any]:
    span = _source_span(path, source, start, end)
    return {
        "binding_kind": "TEXT_SLICE",
        "path": list(path),
        "value_sha256": sha256_bytes(source.encode("utf-8")),
        "text_slice": span,
        "artifact_ref": None,
        "semantic_role": role,
        "visibility_class": visibility_class,
    }


def _region(
    region_name: str,
    kind: str,
    availability: str,
    bindings: list[dict[str, Any]],
    *,
    ownership_role: str = "OWNER",
) -> dict[str, Any]:
    del region_name
    visibility_classes = [
        classification
        for classification in SEMANTIC_REQUEST_VISIBILITY_CLASSES
        if any(binding["visibility_class"] == classification for binding in bindings)
    ]
    identity = canonical_sha256(
        {
            "kind": kind,
            "availability": availability,
            "bindings": bindings,
            "ownership_role": ownership_role,
            "visibility_classes": visibility_classes,
        }
    )
    return {
        "region_id": f"region-{identity[:32]}",
        "kind": kind,
        "availability": availability,
        "bindings": bindings,
        "source_sha256": canonical_sha256(bindings),
        "preserve_exact": True,
        "absence_reason": None,
        "ownership_role": ownership_role,
        "visibility_classes": visibility_classes,
    }


def _semantic_request_leaves(
    value: Any, path: tuple[str | int, ...] = ()
) -> list[tuple[tuple[str | int, ...], Any]]:
    if isinstance(value, Mapping) and value:
        leaves: list[tuple[tuple[str | int, ...], Any]] = []
        for key in sorted(value):
            leaves.extend(_semantic_request_leaves(value[key], (*path, key)))
        return leaves
    if isinstance(value, list) and value:
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(_semantic_request_leaves(item, (*path, index)))
        return leaves
    return [(path, value)]


def _validated_region_binding(
    semantic_request: Mapping[str, Any], binding: Mapping[str, Any]
) -> tuple[str, tuple[str | int, ...], Any]:
    kind = binding.get("binding_kind")
    path_value = binding.get("path")
    visibility_class = binding.get("visibility_class")
    _require(
        kind in {"WHOLE_VALUE", "TEXT_SLICE", "CONTENT_ARTIFACT"}
        and isinstance(path_value, list)
        and bool(path_value)
        and visibility_class in SEMANTIC_REQUEST_VISIBILITY_CLASSES
        and isinstance(binding.get("semantic_role"), str)
        and bool(binding["semantic_role"]),
        "FIELD_VISIBILITY_INVALID",
        "semantic-request binding metadata is invalid",
        stage="VISIBILITY",
    )
    path = tuple(path_value)
    value = _get_path(semantic_request, path)
    if kind == "TEXT_SLICE":
        span = binding.get("text_slice")
        _require(
            isinstance(value, str)
            and isinstance(span, Mapping)
            and span.get("container_path") == list(path)
            and isinstance(span.get("char_start"), int)
            and not isinstance(span.get("char_start"), bool)
            and isinstance(span.get("char_end"), int)
            and not isinstance(span.get("char_end"), bool),
            "REQUEST_PARTITION_INCOMPLETE",
            "text ownership binding does not resolve to one exact string slice",
            stage="VISIBILITY",
            json_path=_json_pointer(path),
        )
        start = span["char_start"]
        end = span["char_end"]
        exact = value[start:end] if 0 <= start < end <= len(value) else None
        _require(
            exact is not None
            and binding.get("value_sha256") == sha256_bytes(value.encode("utf-8"))
            and span.get("utf8_byte_start") == len(value[:start].encode("utf-8"))
            and span.get("utf8_byte_end") == len(value[:end].encode("utf-8"))
            and span.get("exact_text") == exact
            and span.get("span_sha256") == sha256_bytes(exact.encode("utf-8"))
            and binding.get("artifact_ref") is None,
            "REQUEST_HASH_MISMATCH",
            "text ownership binding hash or dual coordinates do not match the semantic request",
            stage="VISIBILITY",
            json_path=_json_pointer(path),
        )
    else:
        _require(
            binding.get("value_sha256") == canonical_sha256(value)
            and binding.get("text_slice") is None
            and (
                (kind == "WHOLE_VALUE" and binding.get("artifact_ref") is None)
                or (kind == "CONTENT_ARTIFACT" and isinstance(binding.get("artifact_ref"), Mapping))
            ),
            "REQUEST_HASH_MISMATCH",
            "whole-value ownership binding hash does not match the semantic request",
            stage="VISIBILITY",
            json_path=_json_pointer(path),
        )
    return kind, path, value


def _validate_semantic_request_partition(
    semantic_request: Mapping[str, Any], regions: Sequence[Mapping[str, Any]]
) -> None:
    """Recompute exact OWNER coverage and the protected tool overlay."""

    _require(
        isinstance(semantic_request, Mapping) and bool(semantic_request),
        "REQUEST_PARTITION_INCOMPLETE",
        "semantic request is not a non-empty mapping",
        stage="VISIBILITY",
    )
    expected_kinds = {
        "SYSTEM",
        "TASK",
        "HISTORY",
        "CURRENT_OBSERVATION",
        "TOOL_PROTOCOL",
        "PROVIDER_CONTROL",
    }
    _require(
        len(regions) == len(expected_kinds)
        and {region.get("kind") for region in regions} == expected_kinds,
        "REQUEST_PARTITION_INCOMPLETE",
        "semantic request does not have exactly one of every required region",
        stage="VISIBILITY",
    )
    owner_bindings: list[tuple[int, int, str, tuple[str | int, ...], Any, Mapping[str, Any]]] = []
    overlay_bindings: list[tuple[str, tuple[str | int, ...], Any, Mapping[str, Any]]] = []
    owner_binding_digests: set[str] = set()
    for region_index, region in enumerate(regions):
        kind = region["kind"]
        bindings = region.get("bindings")
        ownership_role = region.get("ownership_role")
        _require(
            isinstance(bindings, list)
            and bool(bindings)
            and (
                (kind == "TOOL_PROTOCOL" and ownership_role == "PROTECTED_OVERLAY")
                or (kind != "TOOL_PROTOCOL" and ownership_role == "OWNER")
            ),
            "FIELD_VISIBILITY_INVALID",
            "request region owner/overlay role is invalid",
            stage="VISIBILITY",
        )
        actual_classes = [
            classification
            for classification in SEMANTIC_REQUEST_VISIBILITY_CLASSES
            if any(binding.get("visibility_class") == classification for binding in bindings)
        ]
        _require(
            region.get("visibility_classes") == actual_classes
            and region.get("source_sha256") == canonical_sha256(bindings)
            and region.get("preserve_exact") is True
            and region.get("absence_reason") is None,
            "FIELD_VISIBILITY_INVALID",
            "request region visibility or hash metadata is not reproducible",
            stage="VISIBILITY",
        )
        identity = canonical_sha256(
            {
                "kind": kind,
                "availability": region.get("availability"),
                "bindings": bindings,
                "ownership_role": ownership_role,
                "visibility_classes": actual_classes,
            }
        )
        _require(
            region.get("region_id") == f"region-{identity[:32]}",
            "REQUEST_HASH_MISMATCH",
            "request region identity does not match its exact bindings",
            stage="VISIBILITY",
        )
        allowed_classes = {
            "SYSTEM": {"FROZEN_MODEL_VISIBLE"},
            "TASK": {"FROZEN_MODEL_VISIBLE"},
            "HISTORY": {"FROZEN_MODEL_VISIBLE", "MUTABLE_HISTORY_TREATMENT"},
            "CURRENT_OBSERVATION": {"FROZEN_MODEL_VISIBLE"},
            "TOOL_PROTOCOL": {"FROZEN_MODEL_VISIBLE"},
            "PROVIDER_CONTROL": {"FROZEN_NON_HISTORY_ENVELOPE"},
        }[kind]
        _require(
            set(actual_classes) <= allowed_classes
            and (kind != "HISTORY" or "MUTABLE_HISTORY_TREATMENT" in actual_classes),
            "FIELD_VISIBILITY_INVALID",
            "request region contains a forbidden OWNER visibility class",
            stage="VISIBILITY",
        )
        for binding_index, binding in enumerate(bindings):
            validated_kind, path, value = _validated_region_binding(semantic_request, binding)
            if ownership_role == "OWNER":
                owner_bindings.append(
                    (
                        region_index,
                        binding_index,
                        validated_kind,
                        path,
                        value,
                        binding,
                    )
                )
                owner_binding_digests.add(canonical_sha256(binding))
            else:
                overlay_bindings.append((validated_kind, path, value, binding))

    leaves = _semantic_request_leaves(semantic_request)
    used_owner_bindings: set[tuple[int, int]] = set()
    string_partitions: dict[tuple[str | int, ...], list[tuple[int, int, str]]] = {}
    for leaf_path, leaf_value in leaves:
        matching: list[tuple[int, int, str, int, int]] = []
        for region_index, binding_index, kind, path, _value, binding in owner_bindings:
            if kind in {"WHOLE_VALUE", "CONTENT_ARTIFACT"} and leaf_path[: len(path)] == path:
                end = len(leaf_value) if isinstance(leaf_value, str) else 1
                matching.append(
                    (
                        region_index,
                        binding_index,
                        binding["visibility_class"],
                        0,
                        end,
                    )
                )
            elif kind == "TEXT_SLICE" and leaf_path == path:
                span = binding["text_slice"]
                matching.append(
                    (
                        region_index,
                        binding_index,
                        binding["visibility_class"],
                        span["char_start"],
                        span["char_end"],
                    )
                )
        if isinstance(leaf_value, str):
            if not leaf_value:
                _require(
                    len(matching) == 1 and matching[0][3:] == (0, 0),
                    "REQUEST_PARTITION_AMBIGUOUS",
                    "empty semantic-request string does not have exactly one OWNER",
                    stage="VISIBILITY",
                    json_path=_json_pointer(leaf_path),
                )
            else:
                ordered = sorted(matching, key=lambda item: (item[3], item[4], item[0], item[1]))
                cursor = 0
                for entry in ordered:
                    _require(
                        entry[3] == cursor,
                        (
                            "REQUEST_PARTITION_INCOMPLETE"
                            if entry[3] > cursor
                            else "REQUEST_PARTITION_AMBIGUOUS"
                        ),
                        "semantic-request string OWNER slices contain a gap or overlap",
                        stage="VISIBILITY",
                        json_path=_json_pointer(leaf_path),
                    )
                    cursor = entry[4]
                _require(
                    cursor == len(leaf_value),
                    "REQUEST_PARTITION_INCOMPLETE",
                    "semantic-request string OWNER slices do not cover the exact suffix",
                    stage="VISIBILITY",
                    json_path=_json_pointer(leaf_path),
                )
            string_partitions[leaf_path] = [(entry[3], entry[4], entry[2]) for entry in matching]
        else:
            _require(
                len(matching) == 1,
                ("REQUEST_PARTITION_INCOMPLETE" if not matching else "REQUEST_PARTITION_AMBIGUOUS"),
                "semantic-request scalar leaf does not have exactly one OWNER",
                stage="VISIBILITY",
                json_path=_json_pointer(leaf_path),
            )
        used_owner_bindings.update((entry[0], entry[1]) for entry in matching)
    _require(
        len(used_owner_bindings) == len(owner_bindings),
        "REQUEST_PARTITION_AMBIGUOUS",
        "request partition contains an OWNER binding that owns no semantic leaf",
        stage="VISIBILITY",
    )

    _require(
        all(kind == "TEXT_SLICE" for kind, _path, _value, _binding in overlay_bindings)
        and all(
            binding["visibility_class"] == "FROZEN_MODEL_VISIBLE"
            and canonical_sha256(binding) in owner_binding_digests
            for _kind, _path, _value, binding in overlay_bindings
        ),
        "FIELD_VISIBILITY_INVALID",
        "tool protocol overlay is not an exact shared frozen OWNER binding",
        stage="VISIBILITY",
    )
    actual_overlay_coordinates = {
        (
            path,
            binding["text_slice"]["char_start"],
            binding["text_slice"]["char_end"],
        )
        for _kind, path, _value, binding in overlay_bindings
    }
    messages = semantic_request.get("messages")
    _require(
        isinstance(messages, list) and bool(messages),
        "REQUEST_PARTITION_INCOMPLETE",
        "semantic request messages are unavailable for tool overlay validation",
        stage="VISIBILITY",
    )
    system_content = messages[0].get("content")
    if isinstance(system_content, str):
        system_path = ("messages", 0, "content")
        system_text = system_content
    else:
        _require(
            isinstance(system_content, list)
            and len(system_content) == 1
            and isinstance(system_content[0].get("text"), str),
            "REQUEST_PARTITION_INCOMPLETE",
            "system tool protocol text has no exact host coordinate",
            stage="VISIBILITY",
        )
        system_path = ("messages", 0, "content", 0, "text")
        system_text = system_content[0]["text"]
    expected_overlay_coordinates = {(system_path, 0, len(system_text))}
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, Mapping) else None
        if (
            not isinstance(message, Mapping)
            or message.get("role") != "assistant"
            or not isinstance(content, str)
        ):
            continue
        expected_overlay_coordinates.update(
            (("messages", index, "content"), start, end)
            for start, end in _tool_protocol_spans(content)
        )
    _require(
        actual_overlay_coordinates == expected_overlay_coordinates
        and len(actual_overlay_coordinates) == len(overlay_bindings),
        "REQUEST_PARTITION_INCOMPLETE",
        "tool protocol overlay does not bind the exact system/tool-call coordinate set",
        stage="VISIBILITY",
    )
    for _kind, path, _value, binding in overlay_bindings:
        start = binding["text_slice"]["char_start"]
        end = binding["text_slice"]["char_end"]
        owner_segments = sorted(string_partitions[path])
        cursor = start
        for owner_start, owner_end, owner_class in owner_segments:
            if owner_end <= cursor or owner_start >= end:
                continue
            _require(
                owner_start <= cursor and owner_class == "FROZEN_MODEL_VISIBLE",
                "FIELD_VISIBILITY_INVALID",
                "tool protocol overlay intersects mutable or unowned request bytes",
                stage="VISIBILITY",
                json_path=_json_pointer(path),
            )
            cursor = min(end, owner_end)
            if cursor == end:
                break
        _require(
            cursor == end,
            "REQUEST_PARTITION_INCOMPLETE",
            "tool protocol overlay is not fully covered by a frozen OWNER",
            stage="VISIBILITY",
            json_path=_json_pointer(path),
        )


def _validate_targets_inside_history(
    targets: Sequence[Mapping[str, Any]], regions: Sequence[Mapping[str, Any]]
) -> None:
    history = next(region for region in regions if region["kind"] == "HISTORY")
    bindings = history["bindings"]
    for target in targets:
        path = tuple(
            target.get("semantic_container_path", target.get("semantic_request_container_path", ()))
        )
        exposure = target.get("exposure", target.get("exposure_span"))
        _require(
            bool(path) and isinstance(exposure, Mapping),
            "TARGET_SPAN_COORDINATE_MISMATCH",
            "target history coordinate aliases are unavailable",
            stage="REGIONS",
        )
        intervals: list[tuple[int, int, str]] = []
        for binding in bindings:
            binding_path = tuple(binding["path"])
            if binding["binding_kind"] == "TEXT_SLICE" and binding_path == path:
                span = binding["text_slice"]
                intervals.append(
                    (
                        span["char_start"],
                        span["char_end"],
                        binding["visibility_class"],
                    )
                )
            elif (
                binding["binding_kind"] in {"WHOLE_VALUE", "CONTENT_ARTIFACT"}
                and path[: len(binding_path)] == binding_path
            ):
                intervals.append(
                    (
                        0,
                        exposure["char_end"],
                        binding["visibility_class"],
                    )
                )
        _require(
            bool(intervals),
            "TARGET_SPAN_COORDINATE_MISMATCH",
            "target container is outside the declared history region",
            stage="REGIONS",
        )
        for span_kind, span in (
            ("target exposure", exposure),
            *(("focal edit span", focal) for focal in target["focal_edit_spans"]),
        ):
            cursor = span["char_start"]
            for start, end, visibility_class in sorted(intervals):
                if end <= cursor or start >= span["char_end"]:
                    continue
                _require(
                    start <= cursor
                    and (
                        span_kind == "target exposure"
                        or visibility_class == "MUTABLE_HISTORY_TREATMENT"
                    ),
                    "TARGET_SPAN_COORDINATE_MISMATCH",
                    f"{span_kind} intersects protected or unowned history bytes",
                    stage="REGIONS",
                )
                cursor = min(span["char_end"], end)
                if cursor == span["char_end"]:
                    break
            _require(
                cursor == span["char_end"],
                "TARGET_SPAN_COORDINATE_MISMATCH",
                f"{span_kind} falls outside the declared history ownership slices",
                stage="REGIONS",
            )


def _task_parameter_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in (
            "suite_family",
            "task_name",
            "task_index",
            "whole_task_attempt_index",
            "task_goal",
            "task_goal_status",
        )
    }


def _non_history_projection(
    semantic_request: Mapping[str, Any], regions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    projection = _json_clone(semantic_request)
    grouped: defaultdict[tuple[str | int, ...], list[tuple[int, int]]] = defaultdict(list)
    for region in regions:
        if region["ownership_role"] != "OWNER":
            continue
        for binding in region["bindings"]:
            if binding["visibility_class"] != "MUTABLE_HISTORY_TREATMENT":
                continue
            path = tuple(binding["path"])
            source = _get_path(semantic_request, path)
            _require(
                isinstance(source, str),
                "NON_HISTORY_REGION_UNRECOVERABLE",
                "mutable history owner is not an exact string or string slice",
                stage="VISIBILITY",
                json_path=_json_pointer(path),
            )
            if binding["binding_kind"] == "TEXT_SLICE":
                span = binding["text_slice"]
                grouped[path].append((span["char_start"], span["char_end"]))
            else:
                grouped[path].append((0, len(source)))
    for path, spans in grouped.items():
        source = _get_path(projection, path)
        for start, end in sorted(spans, reverse=True):
            source = source[:start] + "<MUTABLE_HISTORY_TREATMENT>" + source[end:]
        parent = _get_path(projection, path[:-1])
        parent[path[-1]] = source
    return projection


def _validate_curation_envelope(source: str, envelope: Any) -> None:
    _require(
        isinstance(envelope, Mapping),
        "TARGET_SPAN_COORDINATE_MISMATCH",
        "curation envelope is not a mapping",
        stage="TARGET",
    )
    start = envelope.get("char_start")
    end = envelope.get("char_end")
    _require_int_range(start, end, len(source), "curation envelope")
    _require(
        envelope.get("editable") is False
        and envelope.get("purpose") == "NON_EDITABLE_G1_6_CURATION_ENVELOPE"
        and envelope.get("utf8_byte_start") == len(source[:start].encode("utf-8"))
        and envelope.get("utf8_byte_end") == len(source[:end].encode("utf-8"))
        and envelope.get("span_sha256") == sha256_bytes(source[start:end].encode("utf-8")),
        "TARGET_SPAN_COORDINATE_MISMATCH",
        "curation envelope does not resolve exactly",
        stage="TARGET",
    )


def _protected_target_spans(source: str, history_family: str) -> list[dict[str, Any]]:
    if history_family != "RAW_REPLAY":
        return []
    protected: list[dict[str, Any]] = []
    for opening, closing in (("<thinking>", "</thinking>"), ("<tool_call>", "</tool_call>")):
        start = source.find(opening)
        end_start = source.find(closing, start + len(opening)) if start >= 0 else -1
        if start >= 0 and end_start >= 0:
            end = end_start + len(closing)
            protected.append(
                {
                    "char_start": start,
                    "char_end": end,
                    "utf8_byte_start": len(source[:start].encode("utf-8")),
                    "utf8_byte_end": len(source[:end].encode("utf-8")),
                    "span_sha256": sha256_bytes(source[start:end].encode("utf-8")),
                }
            )
    return protected


def _max_event_seq(value: Any) -> int:
    maximum = 0
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"event_seq", "seq"} and isinstance(item, int) and not isinstance(item, bool):
                maximum = max(maximum, item)
            else:
                maximum = max(maximum, _max_event_seq(item))
    elif isinstance(value, list):
        for item in value:
            maximum = max(maximum, _max_event_seq(item))
    return maximum


def _verified_blob_summaries(
    value: Mapping[str, Any] | None, blob_store: BlobStore
) -> list[dict[str, Any]]:
    if value is None:
        return []
    found: dict[str, dict[str, Any]] = {}

    def visit(item: Any) -> None:
        if _looks_like_blob_ref(item):
            _require_blob_ref(item, code="BLOB_REFERENCE_INVALID")
            _verify_blob(blob_store, item)
            found[item["digest"]] = _json_clone(item)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return [found[digest] for digest in sorted(found)]


def _safety_flags() -> dict[str, Any]:
    """Return telemetry and distinct downstream authorization guards."""

    return {
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "generated_action_executed": False,
        "raw_collector_mutated": False,
        "collector_labels_added": False,
        "automatic_semantic_inference_performed": False,
        "runtime_sentinel_enabled": False,
        "treatment_response_count": 0,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
    }


def _manifest_safety_flags() -> dict[str, Any]:
    value = _safety_flags()
    value.pop("execution_ready")
    value["g1_1_artifacts_mutated"] = False
    value["g1_2_artifacts_mutated"] = False
    return value


def _population_counts(
    capsules: Sequence[Mapping[str, Any]], exclusions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "target_unit_count": TARGET_POPULATION,
        "capsuled_count": len(capsules),
        "excluded_count": len(exclusions),
        "manifest_unit_count": len(capsules) + len(exclusions),
        "unaccounted_unit_count": TARGET_POPULATION - len(capsules) - len(exclusions),
        "duplicate_unit_count": 0,
        "reserve_unit_capsule_count": 0,
        "g1_1_source_record_mutation_count": 0,
    }


def _media_type_for_path(path: str) -> str:
    if path.endswith(".jsonl"):
        return "application/x-ndjson"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".md"):
        return "text/markdown;charset=utf-8"
    return "application/octet-stream"


def _path_content_ref(
    *, root: Path, path: Path, store_id: str, relative_path: str | None = None
) -> dict[str, Any]:
    data = _read_regular(path)
    relative = relative_path or path.relative_to(root).as_posix()
    _safe_relative(relative, "content reference")
    return {
        "store_id": store_id,
        "relative_path": relative,
        "sha256": sha256_bytes(data),
        "byte_count": len(data),
        "media_type": _media_type_for_path(relative),
    }


def _blob_content_ref(value: Mapping[str, Any], store_id: str) -> dict[str, Any]:
    _require_blob_ref(value, code="BLOB_REFERENCE_INVALID")
    return {
        "store_id": store_id,
        "relative_path": value["relative_path"],
        "sha256": value["digest"],
        "byte_count": value["byte_length"],
        "media_type": value["media_type"],
    }


def _registry_record_ref(unit: RegistryUnit) -> dict[str, Any]:
    return {
        "registry_manifest_sha256": G1_REGISTRY_MANIFEST_SHA256,
        "registry_file_relative_path": unit.registry_file,
        "registry_file_sha256": unit.registry_file_sha256,
        "registry_file_byte_count": unit.registry_file_byte_count,
        "record_index": unit.line_number - 1,
        "record_id": unit.unit_id,
        "record_sha256": unit.line_sha256,
    }


def _manifest_registry_record_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the capsule registry ref to the manifest schema's exact shape."""

    return {
        key: _json_clone(value[key])
        for key in (
            "registry_file_relative_path",
            "registry_file_sha256",
            "registry_file_byte_count",
            "record_index",
            "record_id",
            "record_sha256",
        )
    }


def _schema_unit(unit: RegistryUnit, row: Mapping[str, Any], source_run_id: str) -> dict[str, Any]:
    decision = row["decision"]
    return {
        "unit_kind": unit.unit_kind,
        "unit_id": unit.unit_id,
        "registry_state": (
            row["case_status"] if unit.unit_kind == "STRICT_MHR" else row["control_status"]
        ),
        "registry_record_ref": _registry_record_ref(unit),
        "source_key": row["source_key"],
        "study_role": row["study_role"],
        "model_id": row["model_id"],
        "history_family": row["history_family"].lower(),
        "source_run_id": source_run_id,
        "task_run_id": row["task"]["task_run_id"],
        "stream_id": row["task"]["task_run_id"],
        "step_id": decision["step_id"],
        "target_step": decision["target_step"],
        "request_event_id": decision["request_event_id"],
        "request_id": decision["request_id"],
        "model_call_id": decision["model_call_id"],
        "decision_event_id": decision["decision_event_id"],
        "request_cutoff": {
            "event_id": decision["request_cutoff"]["event_id"],
            "event_seq": decision["request_cutoff"]["event_seq"],
            "monotonic_ns": decision["request_cutoff"]["monotonic_ns"],
            "wall_time": decision["request_cutoff"]["wall_time"],
        },
    }


def _schema_model_provider_provenance(
    *,
    row: Mapping[str, Any],
    repo_root: Path,
    model_record: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    decoding_ref: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and project only pre-cutoff host/model/provider bindings."""

    sdk = request_payload["sdk"]
    endpoint = request_payload["endpoint"]
    captured = model_record["captured_application_request"]
    request_arguments_except_messages = {
        key: _json_clone(value)
        for key, value in request_payload["request_view"].items()
        if key != "messages"
    }
    _require(
        sdk.get("package") == "openai"
        and sdk.get("method") == "chat.completions.create"
        and f"{sdk['package']}.{sdk['method']}" == captured.get("sdk")
        and sdk.get("version") == captured.get("sdk_version")
        and sdk.get("client_configuration", {}).get("max_retries")
        == captured.get("sdk_max_retries")
        and sdk.get("client_configuration", {}).get("timeout", {}).get("all_seconds")
        == captured.get("timeout_seconds")
        and sdk.get("transparent_retry_attempts_observable")
        == captured.get("transparent_http_attempts_observable")
        and endpoint.get("origin") == captured.get("endpoint_origin")
        and endpoint.get("path") == captured.get("endpoint_path")
        and endpoint.get("query_removed") == captured.get("query_removed")
        and request_arguments_except_messages == captured.get("arguments_except_messages")
        and request_arguments_except_messages.get("stream", False) == captured.get("stream")
        and "seed" not in request_arguments_except_messages
        and captured.get("provider_seed_present") is False,
        "REGISTRY_BINDING_INVALID",
        "captured provider envelope differs from the pinned model manifest",
        stage="MODEL_CONFIG",
    )
    model_manifest_ref = _path_content_ref(
        root=repo_root,
        path=repo_root / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
        store_id="REPOSITORY",
        relative_path="mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
    )
    _require(
        model_manifest_ref["sha256"] == MODEL_MANIFEST_SHA256
        and g1_1_canonical_sha256(model_record) == row["model_config_record_sha256"],
        "REGISTRY_BINDING_INVALID",
        "model configuration binding changed",
        stage="MODEL_CONFIG",
    )
    provider = {
        "sdk_package": sdk["package"],
        "sdk_version": sdk["version"],
        "sdk_method": sdk["method"],
        "endpoint_origin": endpoint["origin"],
        "endpoint_path": endpoint["path"],
        "query_removed": endpoint["query_removed"],
        "stream": captured["stream"],
        "decoding_configuration_ref": _json_clone(decoding_ref),
        "decoding_configuration_sha256": canonical_sha256(
            {
                key: value
                for key, value in request_payload["request_view"].items()
                if key != "messages"
            }
        ),
        "excluded_transport_fields": sorted(request_payload["excluded_transport_fields"]),
    }
    _require(
        provider["decoding_configuration_sha256"] == decoding_ref["sha256"],
        "REQUEST_HASH_MISMATCH",
        "captured decoding configuration differs across request projections",
        stage="REQUEST",
    )
    return {
        "host": {
            "adapter_id": "qwen3vl" if row["source_key"] == "qwen" else "mai_ui",
            "component": request_payload["component"],
            "call_role": "actor",
        },
        "model": {
            "model_id": row["model_id"],
            "served_model_name": model_record["served_model_name"],
            "repository": model_record["model_repository"],
            "revision": model_record["model_revision"],
            "model_config_manifest_ref": model_manifest_ref,
            "model_config_manifest_sha256": MODEL_MANIFEST_SHA256,
            "model_config_record_sha256": row["model_config_record_sha256"],
            "actor_adapter_sha256": canonical_sha256(model_record["actor_adapter"]),
            "prompt_implementation_sha256": canonical_sha256(model_record["prompt"]),
            "parser_implementation_sha256": canonical_sha256(model_record["parser_implementation"]),
        },
        "provider": provider,
    }


def _schema_source_provenance(
    *,
    unit: RegistryUnit,
    row: Mapping[str, Any],
    source_base: Path,
    source_spec: Mapping[str, Any],
    run_root: Path,
    stream: EventStream,
    chain: Mapping[str, Any],
    start_manifest: Mapping[str, Any],
    final_manifest: Mapping[str, Any],
    model_provider_provenance: Mapping[str, Any],
    task_projection_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    del unit
    curated_relative = _safe_relative(source_spec["curated_manifest"], "curated manifest")
    curated_path = _safe_child(source_base, curated_relative)
    curated = _load_canonical_object(curated_path)
    curated_ref = _path_content_ref(
        root=source_base,
        path=curated_path,
        store_id="SOURCE_DATASET_ROOT",
        relative_path=curated_relative.as_posix(),
    )
    _require(
        curated_ref["sha256"] == source_spec["curated_manifest_sha256"],
        "SOURCE_HASH_MISMATCH",
        "curated dataset manifest changed",
        stage="SOURCE",
    )
    validation_path = curated_path.parent / "validation_report.json"
    validation = _load_canonical_object(validation_path)
    required_validation_checks = {
        "all_source_task_streams_match_final_manifest",
        "canonical_catalog_is_contiguous_and_unique",
        "exactly_one_eligible_stream_per_task",
        "selected_event_stream_envelopes_and_sequences_valid",
        "selected_task_goals_match_catalog",
        "selected_transitive_blob_paths_and_sizes_valid",
        "selected_transitive_blob_sha256_valid",
        "source_manifests_and_run_stream_hashed",
    }
    _require(
        validation.get("valid") is True
        and validation.get("manifest_sha256") == curated_ref["sha256"]
        and validation.get("dataset_id") == curated.get("dataset_id")
        and validation.get("errors") == []
        and isinstance(validation.get("checks"), Mapping)
        and all(validation["checks"].get(key) is True for key in required_validation_checks)
        and validation["checks"].get("raw_events_or_blobs_copied") is False
        and validation["checks"].get("raw_source_files_written") is False
        and validation["checks"].get("synthetic_run_created") is False,
        "SOURCE_REFERENCE_UNRESOLVED",
        "curated transitive validation receipt is not valid",
        stage="SOURCE",
    )
    validation_ref = _path_content_ref(
        root=source_base,
        path=validation_path,
        store_id="SOURCE_DATASET_ROOT",
    )
    start_ref = _path_content_ref(
        root=run_root,
        path=run_root / "manifest.start.json",
        store_id=run_root.name,
        relative_path="manifest.start.json",
    )
    final_ref = _path_content_ref(
        root=run_root,
        path=run_root / "manifest.final.json",
        store_id=run_root.name,
        relative_path="manifest.final.json",
    )
    stream_ref = _path_content_ref(
        root=run_root,
        path=_safe_child(run_root, PurePosixPath(stream.relative_path)),
        store_id=run_root.name,
        relative_path=stream.relative_path,
    )
    _require(
        stream_ref["sha256"] == stream.sha256,
        "SOURCE_HASH_MISMATCH",
        "task stream content reference changed",
        stage="SOURCE",
    )
    locator = row["frozen_capsule"]["source_locator"]
    curated_sources = [
        record
        for record in curated.get("sources", [])
        if isinstance(record, Mapping)
        and record.get("source_id") == locator["source_id"]
        and record.get("run_id") == locator["source_run_id"]
        and record.get("relative_run_path") == locator["source_relative_run_path"]
    ]
    _require(
        len(curated_sources) == 1,
        "SOURCE_REFERENCE_UNRESOLVED",
        "curated manifest does not uniquely bind the source run",
        stage="SOURCE",
    )
    curated_source = curated_sources[0]
    _require(
        curated_source.get("manifest_start")
        == {
            "relative_path": "manifest.start.json",
            "sha256": start_ref["sha256"],
            "byte_count": start_ref["byte_count"],
        }
        and curated_source.get("manifest_final")
        == {
            "relative_path": "manifest.final.json",
            "sha256": final_ref["sha256"],
            "byte_count": final_ref["byte_count"],
        },
        "SOURCE_HASH_MISMATCH",
        "curated manifest run-manifest binding changed",
        stage="SOURCE",
    )
    task_started = chain["task_started"]
    curated_tasks = [
        record
        for record in curated.get("tasks", [])
        if isinstance(record, Mapping)
        and record.get("source_id") == locator["source_id"]
        and record.get("source_run_id") == locator["source_run_id"]
        and record.get("source_task_run_id") == row["task"]["task_run_id"]
    ]
    _require(
        len(curated_tasks) == 1,
        "SOURCE_REFERENCE_UNRESOLVED",
        "curated manifest does not uniquely bind the task stream",
        stage="SOURCE",
    )
    curated_task = curated_tasks[0]
    _require(
        curated_task.get("task_stream")
        == {
            "relative_path": stream.relative_path,
            "sha256": stream.sha256,
            "byte_count": stream.byte_count,
        }
        and curated_task.get("capture_complete") is True
        and curated_task.get("missing_artifacts") == []
        and curated_task.get("collector_error_event_ids") == []
        and curated_task.get("runtime_status") == "completed"
        and curated_task.get("task_started_event_id") == task_started["event_id"]
        and curated_task.get("task_goal_utf8_sha256")
        == sha256_bytes(task_started["payload"]["task_goal"].encode("utf-8"))
        and curated_task.get("task_goal_utf8_byte_count")
        == len(task_started["payload"]["task_goal"].encode("utf-8")),
        "SOURCE_HASH_MISMATCH",
        "curated manifest task binding changed",
        stage="SOURCE",
    )
    task_payload = task_started["payload"]
    environment = task_payload["environment"]
    instruction = task_payload["task_goal"]
    provenance = {
        "collector_event_schema_version": "mobileworld.audit.event/v1",
        "source_dataset_manifest_ref": curated_ref,
        "source_dataset_manifest_sha256": curated_ref["sha256"],
        "source_run_manifest_start_ref": start_ref,
        "source_run_manifest_final_ref": final_ref,
        "task_stream_ref": stream_ref,
        "task_stream_sha256": stream.sha256,
        "task_started_event": _event_ref(task_started, stream),
        "task": {
            "catalog_index": row["task"]["catalog_index"],
            "task_name": row["task"]["task_name"],
            "instruction": {
                "exact_text": instruction,
                "utf8_sha256": sha256_bytes(instruction.encode("utf-8")),
                "utf8_byte_count": len(instruction.encode("utf-8")),
            },
            "parameter_projection_ref": _json_clone(task_projection_ref),
            "parameter_projection_sha256": row["task"]["task_parameters_sha256"],
            "hidden_benchmark_parameters_runtime_visible": False,
        },
        "environment": {
            "suite_family": "mobile_world",
            "backend_id": environment.get("backend_id"),
            "client": environment.get("client"),
            "client_module": environment.get("client_module"),
            "device_id": environment.get("device_id"),
            "container_image": {
                "status": "CAPTURED",
                "identity": start_manifest.get("environment_image"),
                "source_manifest_bound": True,
            },
            "apk_inventory": _unavailable(
                "APK inventory was not captured and is not a serialized model-call input",
                unavailable=True,
            ),
        },
        "host": _json_clone(model_provider_provenance["host"]),
        "model": _json_clone(model_provider_provenance["model"]),
        "provider": _json_clone(model_provider_provenance["provider"]),
    }
    return provenance, [validation_ref]


def _unavailable(reason: str, *, unavailable: bool = False) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE_FROM_CAPTURE" if unavailable else "ABSENT_IN_CAPTURE",
        "reference": None,
        "canonical_sha256": None,
        "reason": reason,
    }


def _availability_from_value(
    value: Any, *, absent_reason: str, sink: DerivedArtifactSink
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if value is None:
        return _unavailable(absent_reason), []
    reference = sink.put_json(value, media_type="application/json")
    return (
        {
            "status": "AVAILABLE",
            "reference": reference,
            "canonical_sha256": canonical_sha256(value),
            "reason": None,
        },
        [reference],
    )


def _schema_request_images(
    *,
    request_images: Any,
    externalized_request_images: Sequence[Mapping[str, Any]],
    semantic_request: Mapping[str, Any],
    current_path: Sequence[str | int],
    run_id: str,
    blob_store: BlobStore,
) -> list[dict[str, Any]]:
    _require(
        isinstance(request_images, list) and bool(request_images),
        "REQUEST_VIEW_MISMATCH",
        "request image inventory is missing",
        stage="REQUEST",
    )
    current = list(current_path)
    records: list[dict[str, Any]] = []
    seen_paths: set[tuple[str | int, ...]] = set()
    externalized_by_path: dict[tuple[str | int, ...], Mapping[str, Any]] = {}
    for externalized in externalized_request_images:
        _require(
            isinstance(externalized, Mapping)
            and set(externalized)
            == {
                "content_path",
                "semantic_request_path",
                "content_blob",
                "original_text_blob",
            },
            "REQUEST_VIEW_MISMATCH",
            "externalized request-image descriptor is invalid",
            stage="REQUEST",
        )
        externalized_path = tuple(externalized["semantic_request_path"])
        _require(
            list(_parse_dot_path(externalized["content_path"])) == list(externalized_path)
            and externalized_path not in externalized_by_path,
            "REQUEST_VIEW_MISMATCH",
            "externalized request-image coordinate is invalid or duplicated",
            stage="REQUEST",
        )
        _require_blob_ref(externalized["content_blob"], code="BLOB_REFERENCE_INVALID")
        _require_blob_ref(externalized["original_text_blob"], code="BLOB_REFERENCE_INVALID")
        externalized_by_path[externalized_path] = externalized
    for image in request_images:
        _require(
            isinstance(image, Mapping),
            "REQUEST_VIEW_MISMATCH",
            "request image entry is not an object",
            stage="REQUEST",
        )
        path = _parse_dot_path(image.get("content_path"))
        path_key = tuple(path)
        _require(
            path_key not in seen_paths,
            "REQUEST_PARTITION_AMBIGUOUS",
            "request image path is duplicated",
            stage="REQUEST",
        )
        seen_paths.add(path_key)
        content_blob = image.get("content_blob")
        original_blob = image.get("original_text_blob")
        _require_blob_ref(content_blob, code="BLOB_REFERENCE_INVALID")
        _require_blob_ref(original_blob, code="BLOB_REFERENCE_INVALID")
        _verify_blob(blob_store, content_blob)
        _verify_blob(blob_store, original_blob)
        externalized_record = externalized_by_path.get(path_key)
        semantic_value = _get_path(semantic_request, path)
        _require(
            externalized_record is not None
            and externalized_record["content_blob"] == content_blob
            and externalized_record["original_text_blob"] == original_blob
            and isinstance(semantic_value, str)
            and _read_verified_blob(blob_store, original_blob).decode("utf-8") == semantic_value
            and image.get("capture_status") == "captured"
            and image.get("canonical_base64") is True
            and isinstance(image.get("width"), int)
            and not isinstance(image.get("width"), bool)
            and image["width"] > 0
            and isinstance(image.get("height"), int)
            and not isinstance(image.get("height"), bool)
            and image["height"] > 0
            and isinstance(image.get("media_type"), str)
            and image["media_type"].startswith("image/"),
            "REQUEST_VIEW_MISMATCH",
            "request image provenance does not reproduce the semantic request",
            stage="REQUEST",
            json_path=_json_pointer(path),
        )
        records.append(
            {
                "content_path": image["content_path"],
                "semantic_request_path": path,
                "content_blob": _blob_content_ref(content_blob, run_id),
                "original_text_blob": _blob_content_ref(original_blob, run_id),
                "media_type": image["media_type"],
                "width": image["width"],
                "height": image["height"],
                "canonical_base64": True,
                "capture_status": "captured",
                "is_current_observation": path == current,
            }
        )
    _require(
        seen_paths == set(externalized_by_path),
        "REQUEST_VIEW_MISMATCH",
        "Collector request-image inventory does not exactly cover externalized request images",
        stage="REQUEST",
    )
    records.sort(key=lambda record: _typed_path_sort_key(record["semantic_request_path"]))
    _require(
        sum(record["is_current_observation"] for record in records) == 1,
        "CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED",
        "request image inventory has no unique current-observation coordinate",
        stage="REQUEST",
    )
    return records


def _runtime_replay_binding(
    source_provenance: Mapping[str, Any],
    parser_implementation_ref: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the exact provider-authorizing pins into the harness allowlist."""

    host = source_provenance["host"]
    model = source_provenance["model"]
    provider = source_provenance["provider"]
    return {
        "binding_version": "mobileworld.g1.replay-binding/v1",
        "host": {key: _json_clone(host[key]) for key in ("adapter_id", "component", "call_role")},
        "model": {
            key: _json_clone(model[key])
            for key in (
                "model_id",
                "served_model_name",
                "repository",
                "revision",
                "model_config_manifest_sha256",
                "model_config_record_sha256",
            )
        },
        "provider": {
            key: _json_clone(provider[key])
            for key in (
                "sdk_package",
                "sdk_version",
                "sdk_method",
                "endpoint_origin",
                "endpoint_path",
                "query_removed",
                "stream",
                "decoding_configuration_ref",
                "decoding_configuration_sha256",
                "excluded_transport_fields",
            )
        }
        | {"excluded_transport_fields_send_eligible": False},
        "parser": {
            "binding_id": (f"{model['model_id']}:production-next-action-parser/v1"),
            "implementation_ref": _json_clone(parser_implementation_ref),
            "implementation_sha256": model["parser_implementation_sha256"],
        },
    }


def _schema_non_history_envelope(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    stream: EventStream,
    current_state: Mapping[str, Any],
    request: Mapping[str, Any],
    run_id: str,
    observation_ref: Mapping[str, Any],
    decoding_configuration: Mapping[str, Any],
    replay_binding: Mapping[str, Any],
    sink: DerivedArtifactSink,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    observation = chain["pre"]["payload"]["observation"]
    screenshot = observation["screenshot"]
    pixel_ref = _blob_content_ref(screenshot["pixel_blob"], run_id)
    source_ref = (
        _blob_content_ref(screenshot["source_blob"], run_id)
        if screenshot.get("source_blob") is not None
        else None
    )
    path = current_state["observation"]["screenshot"]["request_semantic_path"]
    current_digest = screenshot["pixel_blob"]["digest"]
    current_dot = _dot_path(path)
    same_digest_paths = sorted(
        [
            _parse_dot_path(image["content_path"])
            for image in request["payload"]["request_images"]
            if image["content_blob"]["digest"] == current_digest
            and image["content_path"] != current_dot
        ],
        key=_typed_path_sort_key,
    )
    tool_state, tool_refs = _availability_from_value(
        observation.get("tool_call"),
        absent_reason="No tool-call state was present in the captured pre-call observation",
        sink=sink,
    )
    ask_state, ask_refs = _availability_from_value(
        observation.get("ask_user_response"),
        absent_reason="No ask-user response was present in the captured pre-call observation",
        sink=sink,
    )
    original_text_ref = _blob_content_ref(
        current_state["observation"]["screenshot"]["original_text_blob"], run_id
    )
    envelope = {
        "current_state_event": _pre_cutoff_event_ref(chain["pre"], stream),
        "canonical_observation_ref": _json_clone(observation_ref),
        "state_sha256": canonical_sha256(observation),
        "current_screenshot": {
            "pixel_blob": pixel_ref,
            "source_blob": source_ref,
            "width": screenshot["width"],
            "height": screenshot["height"],
            "mode": screenshot["mode"],
            "semantic_request_path": path,
            "request_path_selection_rule": (
                "UNIQUE_CURRENT_BLOCK_COORDINATE"
                if row["history_family"] == "FLAT_PROGRESS"
                else "HOST_DECLARED_LATEST_CURRENT_BLOCK"
            ),
            "same_digest_at_other_request_paths": same_digest_paths,
        },
        "ui_tree": _unavailable("Collector v1 recorded accessibility_tree=null for this decision"),
        "tool_state": tool_state,
        "ask_user_state": ask_state,
        "last_causally_available_transition": _pre_cutoff_event_ref(
            chain["last_transition"], stream
        ),
        "provider_envelope_sha256": canonical_sha256(decoding_configuration),
        "replay_binding": _json_clone(replay_binding),
        "restore_descriptor": {
            "mode": "SERIALIZED_REQUEST_ONLY",
            "external_state_consulted": False,
            "checkpoint_required": False,
            "proof_model_config_sha256": MODEL_MANIFEST_SHA256,
            "checkpoint_ref": None,
            "prefix_recipe": None,
        },
    }
    refs = [
        pixel_ref,
        original_text_ref,
        _json_clone(replay_binding["parser"]["implementation_ref"]),
        *tool_refs,
        *ask_refs,
    ]
    if source_ref is not None:
        refs.append(source_ref)
    return envelope, refs


def _parse_dot_path(value: str) -> list[str | int]:
    _require(
        isinstance(value, str) and bool(value),
        "REQUEST_PARTITION_INCOMPLETE",
        "request content path is invalid",
        stage="REQUEST",
    )
    parts: list[str | int] = []
    for name, index in re.findall(r"(?:^|\.)([^.\[]+)|\[(\d+)\]", value):
        parts.append(int(index) if index else name)
    _require(
        _dot_path(parts) == value,
        "REQUEST_PARTITION_INCOMPLETE",
        "request content path is not canonical",
        stage="REQUEST",
    )
    return parts


def _schema_treatment_surface(
    *,
    row: Mapping[str, Any],
    semantic_request: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_records: dict[tuple[str | int, ...], dict[str, Any]] = {}
    target_exposures: list[dict[str, Any]] = []
    for target in targets:
        path = tuple(target["semantic_request_container_path"])
        source = _get_path(semantic_request, path)
        _require(
            isinstance(source, str),
            "TARGET_SPAN_UNRESOLVED",
            "target source record is not text",
            stage="TARGET",
        )
        message_index = target["message_index"]
        message = _get_path(semantic_request, ("messages", message_index))
        identity = {
            "container_path": list(path),
            "record_sha256": sha256_bytes(source.encode("utf-8")),
        }
        source_records[path] = {
            "record_id": f"record-{canonical_sha256(identity)[:32]}",
            "container_path": list(path),
            "message_index": message_index,
            "content_block_index": target["content_block_index"],
            "author_role": message["role"],
            "exact_text": source,
            "record_sha256": target["container_sha256"],
        }
        enclosing_record_span = None
        if row["history_family"] == "FLAT_PROGRESS":
            step = target["source_steps"][-1]
            marker = f"Step {step}: "
            exposure_start = target["exposure_span"]["char_start"]
            exposure_end = target["exposure_span"]["char_end"]
            record_start = source.rfind(marker, 0, exposure_start + 1)
            _require(
                record_start >= 0 and record_start + len(marker) == exposure_start,
                "TARGET_SPAN_COORDINATE_MISMATCH",
                "flat-progress target is not bound to its exact enclosing Step record",
                stage="TARGET",
            )
            record_end = exposure_end + (1 if source[exposure_end:].startswith(";") else 0)
            enclosing_record_span = _source_span(path, source, record_start, record_end)
        target_exposures.append(
            {
                key: _json_clone(target[key])
                for key in (
                    "candidate_id",
                    "provenance_confidence",
                    "source_steps",
                    "registry_request_path",
                    "semantic_request_container_path",
                    "record_identity_sha256",
                    "container_sha256",
                    "message_index",
                    "content_block_index",
                    "record_index",
                    "exposure_span",
                    "edit_span_status",
                    "focal_edit_spans",
                    "curation_envelope",
                    "transform_binding",
                )
            }
            | {"enclosing_record_span": enclosing_record_span}
        )
    records = [source_records[path] for path in sorted(source_records, key=_typed_path_sort_key)]
    target_exposures.sort(
        key=lambda target: (
            _typed_path_sort_key(target["semantic_request_container_path"]),
            target["exposure_span"]["char_start"],
            target["exposure_span"]["char_end"],
            target["candidate_id"],
        )
    )
    return {
        "history_family": row["history_family"].lower(),
        "source_records": records,
        "target_exposures": target_exposures,
        "ordered_target_set_sha256": canonical_sha256(target_exposures),
        "treatment_plan_present": False,
        "treatment_execution_ready": False,
    }


def _evidence_ref(
    *,
    role: str,
    content_ref: Mapping[str, Any],
    event: Mapping[str, Any],
    stream: EventStream,
) -> dict[str, Any]:
    identity = {
        "role": role,
        "sha256": content_ref["sha256"],
        "event_id": event["event_id"],
    }
    return {
        "evidence_id": f"evidence-{canonical_sha256(identity)[:24]}",
        "evidence_role": role,
        "content_ref": _json_clone(content_ref),
        "source_event": _pre_cutoff_event_ref(event, stream),
        "temporal_relation": "AT_OR_BEFORE_REQUEST_CUTOFF",
        "model_visible_at_or_before_request": True,
    }


def _schema_curator_channels(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    stream: EventStream,
    targets: Sequence[Mapping[str, Any]],
    task_instruction: str,
    observation_ref: Mapping[str, Any],
    sink: DerivedArtifactSink,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    task_ref = sink.put_text(task_instruction, media_type="text/plain;charset=utf-8")
    task_evidence = _evidence_ref(
        role="task_instruction",
        content_ref=task_ref,
        event=chain["task_started"],
        stream=stream,
    )
    current_evidence = _evidence_ref(
        role="target_pre",
        content_ref=observation_ref,
        event=chain["pre"],
        stream=stream,
    )
    transformation_evidence = [task_evidence, current_evidence]
    refs: list[dict[str, Any]] = [task_ref, _json_clone(observation_ref)]
    for target in targets:
        history_ref = sink.put_text(
            target["exposure_span"]["exact_text"],
            media_type="text/plain;charset=utf-8",
        )
        refs.append(history_ref)
        transformation_evidence.append(
            _evidence_ref(
                role="source_history",
                content_ref=history_ref,
                event=chain["request"],
                stream=stream,
            )
        )
    observation = chain["pre"]["payload"]["observation"]
    optional_roles = (
        ("tool_response", observation.get("tool_call")),
        ("ask_user_response", observation.get("ask_user_response")),
    )
    action_evidence = [task_evidence, current_evidence]
    for role, value in optional_roles:
        if value is None:
            continue
        reference = sink.put_json(value, media_type="application/json")
        refs.append(reference)
        evidence = _evidence_ref(
            role=role,
            content_ref=reference,
            event=chain["pre"],
            stream=stream,
        )
        action_evidence.append(evidence)
        transformation_evidence.append(evidence)
    cutoff = row["decision"]["request_cutoff"]["event_seq"]
    _require(
        all(
            evidence["source_event"]["seq"] <= cutoff
            for evidence in (*action_evidence, *transformation_evidence)
        ),
        "FUTURE_EVIDENCE_LEAKAGE",
        "curator evidence exceeds the request cutoff",
        stage="VISIBILITY",
    )
    action_evidence.sort(key=lambda item: (item["evidence_role"], item["evidence_id"]))
    transformation_evidence.sort(key=lambda item: (item["evidence_role"], item["evidence_id"]))
    return (
        {
            "action_gold": {
                "channel": "ACTION_GOLD",
                "status": "G1_6_PENDING",
                "evidence_refs": action_evidence,
                "history_visible": False,
                "natural_target_output_visible": False,
                "later_trajectory_visible": False,
                "event_cutoff_policy": "AT_OR_BEFORE_REQUEST_CUTOFF",
            },
            "transformation": {
                "channel": "TRANSFORMATION",
                "status": "G1_6_PENDING",
                "evidence_refs": transformation_evidence,
                "history_visible": True,
                "natural_target_output_visible": False,
                "later_trajectory_visible": False,
                "event_cutoff_policy": "AT_OR_BEFORE_REQUEST_CUTOFF",
            },
        },
        refs,
    )


def _all_blob_content_refs(
    value: Any, *, run_id: str, blob_store: BlobStore
) -> list[dict[str, Any]]:
    references: dict[tuple[str, str], dict[str, Any]] = {}
    expanded_graphs: set[str] = set()

    def visit(item: Any) -> None:
        if _looks_like_blob_ref(item):
            _require_blob_ref(item, code="BLOB_REFERENCE_INVALID")
            _verify_blob(blob_store, item)
            reference = _blob_content_ref(item, run_id)
            references[(reference["relative_path"], reference["sha256"])] = reference
            if (
                item.get("media_type") == "application/vnd.mobileworld.audit.artifact+json"
                and item["digest"] not in expanded_graphs
            ):
                expanded_graphs.add(item["digest"])
                graph_bytes = _read_verified_blob(blob_store, item)
                graph = _parse_json_bytes(graph_bytes, Path(item["relative_path"]))
                visit(graph)
            return
        if isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return [references[key] for key in sorted(references)]


def _schema_post_action_audit(
    *,
    row: Mapping[str, Any],
    chain: Mapping[str, Any],
    stream: EventStream,
    run_id: str,
    blob_store: BlobStore,
    sink: DerivedArtifactSink,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    response = chain["response"]
    decision = chain["decision"]
    execution = chain["execution"]
    terminal = chain["terminal"]
    normalized = response["payload"].get("normalized_response")
    _require(
        normalized is not None,
        "ORIGINAL_RESPONSE_UNRESOLVED",
        "captured model response has no normalized payload",
        stage="POST_ACTION",
    )
    normalized_ref = sink.put_json(normalized, media_type="application/json")
    parsed_action = decision["payload"].get("parsed_action")
    _require(
        parsed_action is not None
        and parsed_action == row["frozen_capsule"]["original_action"]["parsed_action"]
        and g1_1_canonical_sha256(parsed_action)
        == row["frozen_capsule"]["original_action"]["parsed_action_sha256"],
        "ORIGINAL_ACTION_UNRESOLVED",
        "captured natural action differs from the frozen G1.1 reference",
        stage="POST_ACTION",
    )
    parsed_ref = sink.put_bytes(canonical_json_line(parsed_action), media_type="application/json")
    prediction = decision["payload"].get("prediction_raw")
    prediction_ref = (
        sink.put_text(prediction, media_type="text/plain;charset=utf-8")
        if isinstance(prediction, str)
        else None
    )
    raw_response_refs = _all_blob_content_refs(response, run_id=run_id, blob_store=blob_store)
    refs = [normalized_ref, parsed_ref, *raw_response_refs]
    if prediction_ref is not None:
        refs.append(prediction_ref)
    if execution is None:
        execution_audit = {
            "transition_kind": "NOT_EXECUTED",
            "action_execution_event": None,
            "transition_event": _event_ref(terminal, stream),
            "executor_result_ref": None,
            "post_state_ref": None,
            "post_state_sha256": None,
        }
        suffix_events = [response, decision, terminal]
    else:
        _require(
            terminal["event_type"] == "transition_completed",
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "executed target did not end in a completed transition",
            stage="POST_ACTION",
        )
        result = terminal["payload"].get("execution_result")
        post_state = terminal["payload"].get("post_observation")
        _require(
            result is not None and post_state is not None,
            "ORIGINAL_TRANSITION_UNRESOLVED",
            "completed transition lacks result or post-state",
            stage="POST_ACTION",
        )
        result_ref = sink.put_json(result, media_type="application/json")
        post_ref = sink.put_json(post_state, media_type="application/json")
        refs.extend([result_ref, post_ref])
        refs.extend(_all_blob_content_refs(execution, run_id=run_id, blob_store=blob_store))
        refs.extend(_all_blob_content_refs(terminal, run_id=run_id, blob_store=blob_store))
        execution_audit = {
            "transition_kind": "COMPLETED",
            "action_execution_event": _event_ref(execution, stream),
            "transition_event": _event_ref(terminal, stream),
            "executor_result_ref": result_ref,
            "post_state_ref": post_ref,
            "post_state_sha256": canonical_sha256(post_state),
        }
        suffix_events = [response, decision, execution, terminal]
    audit = {
        "runtime_eligible": False,
        "curator_eligible": False,
        "historical_reference_only": True,
        "original_response": {
            "terminal_event": _event_ref(response, stream),
            "terminal_kind": "RETURNED",
            "raw_artifact_refs": raw_response_refs,
            "normalized_response_ref": normalized_ref,
            "response_sha256": canonical_sha256(normalized),
        },
        "natural_decision": {
            "decision_event": _event_ref(decision, stream),
            "parse_outcome": decision["payload"]["parse_outcome"],
            "prediction_ref": prediction_ref,
            "parsed_action_ref": parsed_ref,
            "parsed_action_sha256": g1_1_canonical_sha256(parsed_action),
            "replay_expectation_role": "DESCRIPTIVE_REFERENCE_ONLY",
        },
        "execution": execution_audit,
        "audit_suffix_sha256": canonical_sha256(suffix_events),
    }
    return audit, refs


def _graph_blob_refs(
    graph: Mapping[str, Any], run_id: str, blob_store: BlobStore
) -> list[dict[str, Any]]:
    return _all_blob_content_refs(graph, run_id=run_id, blob_store=blob_store)


def _looks_like_content_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {
        "store_id",
        "relative_path",
        "sha256",
        "byte_count",
        "media_type",
    }


def _schema_artifact_closure(*, section_values: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
    entries: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def collect(section: str, value: Any) -> None:
        if _looks_like_content_ref(value):
            reference = _json_clone(value)
            key = (
                section,
                reference["store_id"],
                reference["relative_path"],
                reference["sha256"],
            )
            entries[key] = {
                "section": section,
                "reference": reference,
                "verified": True,
            }
            return
        if isinstance(value, Mapping):
            for child in value.values():
                collect(section, child)
        elif isinstance(value, list) or isinstance(value, tuple):
            for child in value:
                collect(section, child)

    for section, values in section_values.items():
        for value in values:
            collect(section, value)
    ordered = [entries[key] for key in sorted(entries)]
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in ordered:
        reference = entry["reference"]
        unique[
            (
                reference["store_id"],
                reference["relative_path"],
                reference["sha256"],
            )
        ] = reference
    _require(
        bool(ordered),
        "BLOB_REFERENCE_INVALID",
        "capsule artifact closure is empty",
        stage="ARTIFACT_CLOSURE",
    )
    return {
        "entries": ordered,
        "unique_artifact_count": len(unique),
        "total_byte_count": sum(reference["byte_count"] for reference in unique.values()),
        "aggregate_sha256": canonical_sha256(ordered),
    }


def _source_closure_sha256(closure: Mapping[str, Any]) -> str:
    references: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in closure["entries"]:
        reference = entry["reference"]
        if reference["store_id"] == "G1_3_PUBLICATION":
            continue
        key = (
            reference["store_id"],
            reference["relative_path"],
            reference["sha256"],
        )
        references[key] = reference
    return canonical_sha256([references[key] for key in sorted(references)])


def _validate_artifact_closure(capsule: Mapping[str, Any]) -> None:
    """Prove every direct section reference is present in the sealed closure."""

    closure = capsule["artifact_closure"]
    closure_keys: set[tuple[str, str, str, str]] = set()
    unique_references: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for entry in closure["entries"]:
        reference = entry["reference"]
        key = (
            entry["section"],
            reference["store_id"],
            reference["relative_path"],
            reference["sha256"],
        )
        _require(
            key not in closure_keys,
            "BLOB_REFERENCE_INVALID",
            "artifact closure contains a duplicate classified reference",
            stage="ARTIFACT_CLOSURE",
        )
        closure_keys.add(key)
        unique_references[
            (
                reference["store_id"],
                reference["relative_path"],
                reference["sha256"],
            )
        ] = reference

    missing: list[tuple[str, str, str, str]] = []

    def collect(section: str, value: Any) -> None:
        if _looks_like_content_ref(value):
            key = (
                section,
                value["store_id"],
                value["relative_path"],
                value["sha256"],
            )
            if key not in closure_keys:
                missing.append(key)
            return
        if isinstance(value, Mapping):
            for child in value.values():
                collect(section, child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(section, child)

    section_values = {
        "SOURCE_PROVENANCE": capsule["source_provenance"],
        "FROZEN_MODEL_VISIBLE": capsule["runtime"]["model_visible"],
        "FROZEN_NON_HISTORY_ENVELOPE": capsule["runtime"]["non_history_envelope"],
        "MUTABLE_HISTORY_TREATMENT": capsule["runtime"]["treatment_surface"],
        "CURATOR_ONLY": capsule["curator_only"],
        "POST_ACTION_AUDIT_ONLY": capsule["post_action_audit"],
    }
    for section, value in section_values.items():
        collect(section, value)
    _require(
        not missing
        and closure["unique_artifact_count"] == len(unique_references)
        and closure["total_byte_count"]
        == sum(reference["byte_count"] for reference in unique_references.values())
        and closure["aggregate_sha256"] == canonical_sha256(closure["entries"]),
        "BLOB_REFERENCE_INVALID",
        "artifact closure omits a direct reference or has invalid aggregate metadata",
        stage="ARTIFACT_CLOSURE",
    )


def _verify_capsule_source_closure(
    capsules: Sequence[Mapping[str, Any]], *, repo_root: Path, source_base: Path
) -> str:
    """Re-read every source ref reachable from the capsuled population."""

    run_roots: dict[str, Path] = {}
    references: dict[tuple[str, str], dict[str, Any]] = {}
    for envelope in capsules:
        capsule = envelope["capsule"]
        provenance = capsule["source_provenance"]
        dataset_ref = provenance["source_dataset_manifest_ref"]
        dataset_path = _safe_child(
            source_base,
            _safe_relative(dataset_ref["relative_path"], "source dataset manifest"),
        )
        dataset = _load_canonical_object(dataset_path)
        run_id = capsule["unit"]["source_run_id"]
        matching = [
            record
            for record in dataset.get("sources", [])
            if isinstance(record, Mapping) and record.get("run_id") == run_id
        ]
        _require(
            len(matching) == 1,
            "SOURCE_REFERENCE_UNRESOLVED",
            "source run root is not uniquely recoverable from curated provenance",
            stage="SOURCE",
        )
        relative_run = _safe_relative(matching[0]["relative_run_path"], "source run root")
        run_root = _safe_child(source_base, relative_run)
        _require(
            run_root.is_dir() and not run_root.is_symlink(),
            "SOURCE_REFERENCE_UNRESOLVED",
            "source run root is not a real directory",
            stage="SOURCE",
        )
        existing = run_roots.get(run_id)
        _require(
            existing is None or existing == run_root,
            "SOURCE_REFERENCE_UNRESOLVED",
            "source run id resolves to multiple roots",
            stage="SOURCE",
        )
        run_roots[run_id] = run_root
        for entry in capsule["artifact_closure"]["entries"]:
            reference = entry["reference"]
            if reference["store_id"] == "G1_3_PUBLICATION":
                continue
            key = (reference["store_id"], reference["relative_path"])
            previous = references.get(key)
            _require(
                previous is None or previous == reference,
                "SOURCE_HASH_MISMATCH",
                "one source locator resolves to conflicting content references",
                stage="SOURCE",
            )
            references[key] = reference
    verified: dict[str, dict[str, Any]] = {}
    for (store_id, relative_path), reference in sorted(references.items()):
        if store_id == "REPOSITORY":
            root = repo_root
        elif store_id == "SOURCE_DATASET_ROOT":
            root = source_base
        else:
            root = run_roots.get(store_id)
            _require(
                root is not None,
                "SOURCE_REFERENCE_UNRESOLVED",
                "artifact store id has no frozen source root",
                stage="SOURCE",
            )
        path = _safe_child(root, _safe_relative(relative_path, "source artifact"))
        data = _read_regular(path)
        _require(
            sha256_bytes(data) == reference["sha256"] and len(data) == reference["byte_count"],
            "SOURCE_HASH_MISMATCH",
            "referenced source artifact changed",
            stage="SOURCE",
            json_path=f"/{store_id}/{relative_path}",
        )
        verified[f"{store_id}:{relative_path}"] = {
            "sha256": reference["sha256"],
            "byte_count": reference["byte_count"],
        }
    _require(
        bool(verified),
        "SOURCE_REFERENCE_UNRESOLVED",
        "source closure is empty",
        stage="SOURCE",
    )
    return canonical_sha256(verified)


def _snapshot_population_source_files(
    *,
    population: Sequence[RegistryUnit],
    source_by_key: Mapping[str, Mapping[str, Any]],
    source_base: Path,
) -> str:
    """Hash the complete raw/curated file closure before or after a build."""

    files: dict[str, dict[str, Any]] = {}

    def add_file(
        *,
        root: Path,
        relative: PurePosixPath,
        store_id: str,
        expected_sha256: str | None = None,
        expected_byte_count: int | None = None,
    ) -> bytes | None:
        key = f"{store_id}:{relative.as_posix()}"
        try:
            path = _safe_child(root, relative)
            data = _read_regular(path)
        except (OSError, ReplayCapsuleError) as error:
            reason_code = (
                error.code
                if isinstance(error, ReplayCapsuleError)
                else "SOURCE_REFERENCE_UNRESOLVED"
            )
            files[key] = {
                "status": "UNAVAILABLE",
                "reason_code": reason_code,
                "expected_sha256": expected_sha256,
                "expected_byte_count": expected_byte_count,
            }
            return None
        digest = sha256_bytes(data)
        matches_reference = (expected_sha256 is None or digest == expected_sha256) and (
            expected_byte_count is None or len(data) == expected_byte_count
        )
        summary = {
            "status": "AVAILABLE" if matches_reference else "FROZEN_REFERENCE_MISMATCH",
            "sha256": digest,
            "byte_count": len(data),
            "expected_sha256": expected_sha256,
            "expected_byte_count": expected_byte_count,
        }
        previous = files.get(key)
        _require(
            previous is None or previous == summary,
            "SOURCE_HASH_MISMATCH",
            "source snapshot locator resolves to conflicting bytes",
            stage="SOURCE",
        )
        files[key] = summary
        return data

    for source_key, specification in sorted(source_by_key.items()):
        curated_relative = _safe_relative(specification["curated_manifest"], "curated manifest")
        curated_bytes = add_file(
            root=source_base,
            relative=curated_relative,
            store_id="SOURCE_DATASET_ROOT",
            expected_sha256=specification["curated_manifest_sha256"],
        )
        curated: Mapping[str, Any] | None = None
        if curated_bytes is not None:
            try:
                curated = _load_canonical_object_bytes(
                    curated_bytes, _safe_child(source_base, curated_relative)
                )
            except (OSError, ReplayCapsuleError):
                curated = None
        validation_relative = curated_relative.parent / "validation_report.json"
        validation_bytes = add_file(
            root=source_base,
            relative=validation_relative,
            store_id="SOURCE_DATASET_ROOT",
        )
        if validation_bytes is not None:
            try:
                _load_canonical_object_bytes(
                    validation_bytes, _safe_child(source_base, validation_relative)
                )
            except (OSError, ReplayCapsuleError):
                pass
        review_relative = _safe_relative(
            f"{specification['audit_root']}/{specification['final_reviews_relative_path']}",
            "frozen final reviews",
        )
        add_file(
            root=source_base,
            relative=review_relative,
            store_id="SOURCE_DATASET_ROOT",
            expected_sha256=specification["final_reviews_sha256"],
        )
        del curated, source_key

    unique_streams: dict[tuple[str, str], Mapping[str, Any]] = {}
    for unit in population:
        locator = unit.record["frozen_capsule"]["source_locator"]
        key = (
            locator["source_relative_run_path"],
            locator["task_stream_relative_path"],
        )
        previous = unique_streams.get(key)
        _require(
            previous is None or previous == locator,
            "SOURCE_REFERENCE_UNRESOLVED",
            "one source stream locator has conflicting frozen identities",
            stage="SOURCE",
        )
        unique_streams[key] = locator

    expanded_blobs: set[tuple[str, str]] = set()
    for (run_relative_text, stream_relative_text), locator in sorted(unique_streams.items()):
        run_relative = _safe_relative(run_relative_text, "source run")
        stream_relative = _safe_relative(stream_relative_text, "task stream")
        run_root = source_base.joinpath(*run_relative.parts)
        run_id = locator["source_run_id"]
        add_file(
            root=run_root,
            relative=PurePosixPath("manifest.start.json"),
            store_id=run_id,
        )
        add_file(
            root=run_root,
            relative=PurePosixPath("manifest.final.json"),
            store_id=run_id,
        )
        stream_bytes = add_file(
            root=run_root,
            relative=stream_relative,
            store_id=run_id,
            expected_sha256=locator["task_stream_sha256"],
        )
        if stream_bytes is None:
            continue
        queue: list[Mapping[str, Any]] = []

        def discover(value: Any) -> None:
            if _looks_like_blob_ref(value):
                queue.append(value)
            elif isinstance(value, Mapping):
                for child in value.values():
                    discover(child)
            elif isinstance(value, list):
                for child in value:
                    discover(child)

        for line_number, line in enumerate(stream_bytes.splitlines(keepends=True), start=1):
            try:
                event = _load_canonical_line_bytes(
                    line, _safe_child(run_root, stream_relative), line_number
                )
            except (OSError, ReplayCapsuleError):
                break
            discover(event)
        while queue:
            reference = queue.pop()
            try:
                _require_blob_ref(reference, code="BLOB_REFERENCE_INVALID")
                blob_relative = _safe_relative(reference["relative_path"], "blob")
            except ReplayCapsuleError:
                continue
            blob_key = (run_id, reference["relative_path"])
            if blob_key in expanded_blobs:
                continue
            expanded_blobs.add(blob_key)
            blob_bytes = add_file(
                root=run_root,
                relative=blob_relative,
                store_id=run_id,
                expected_sha256=reference["digest"],
                expected_byte_count=reference["byte_length"],
            )
            if blob_bytes is None:
                continue
            if reference["media_type"] == "application/vnd.mobileworld.audit.artifact+json":
                try:
                    graph = _parse_json_bytes(blob_bytes, Path(reference["relative_path"]))
                except ReplayCapsuleError:
                    continue
                discover(graph)
    _require(
        bool(files),
        "SOURCE_REFERENCE_UNRESOLVED",
        "source snapshot is empty",
        stage="SOURCE",
    )
    return canonical_sha256(files)


def _validate_visibility_boundary(capsule: Mapping[str, Any]) -> None:
    cutoff = capsule["unit"]["request_cutoff"]["event_seq"]
    runtime = capsule["runtime"]
    curator = capsule["curator_only"]
    full_stream_sha256 = capsule["source_provenance"]["task_stream_sha256"]

    def contains_full_stream_alias(value: Any) -> bool:
        if isinstance(value, Mapping):
            if "task_stream_sha256" in value:
                return True
            return any(contains_full_stream_alias(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_full_stream_alias(child) for child in value)
        return value == full_stream_sha256

    _require(
        _max_event_seq(runtime) <= cutoff and _max_event_seq(curator) <= cutoff,
        "FUTURE_EVIDENCE_LEAKAGE",
        "runtime or curator projection contains a post-cutoff event",
        stage="VISIBILITY",
    )
    _require(
        not contains_full_stream_alias(runtime) and not contains_full_stream_alias(curator),
        "FUTURE_EVIDENCE_LEAKAGE",
        "runtime or curator projection aliases the future-bearing full task stream",
        stage="VISIBILITY",
    )
    _require(
        "post_action_audit" not in runtime
        and "post_action_audit" not in curator
        and capsule["post_action_audit"]["runtime_eligible"] is False
        and capsule["post_action_audit"]["curator_eligible"] is False,
        "FIELD_VISIBILITY_INVALID",
        "sealed post-action audit data is exposed to a restricted consumer",
        stage="VISIBILITY",
    )


def _cli_summary(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    manifest_bytes = artifacts["file_payloads"]["capsule_manifest.json"]
    return {
        "valid": True,
        "builder_version": BUILDER_VERSION,
        "contract_amendment_version": CONTRACT_AMENDMENT_VERSION,
        "capsule_schema_version": CAPSULE_SCHEMA_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "integrity_schema_version": INTEGRITY_SCHEMA_VERSION,
        "publication_phase": artifacts["manifest"]["publication_phase"],
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "capsule_set_sha256": artifacts["manifest"]["capsule_set_sha256"],
        "capsuled_count": artifacts["manifest"]["counts"]["capsuled_count"],
        "excluded_count": artifacts["manifest"]["counts"]["excluded_count"],
        "file_count": len(artifacts["file_payloads"]),
        "total_byte_count": sum(len(data) for data in artifacts["file_payloads"].values()),
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "raw_collector_mutated": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CPU-only CLI for candidate build, formal verification, and publication."""

    repo_default = Path(__file__).resolve().parents[4]
    registry_default = (
        "/shared/linqiang/mobileworld_causal_replay_data/g1_1/registry/sha256/"
        + G1_REGISTRY_MANIFEST_SHA256
    )
    source_default = "/shared/linqiang/mobileworld_audit_data"
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_sources(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo-root", default=str(repo_default))
        command.add_argument("--registry-root", default=registry_default)
        command.add_argument("--source-base", default=source_default)

    candidate = subparsers.add_parser("candidate")
    add_sources(candidate)
    verify = subparsers.add_parser("verify")
    add_sources(verify)
    publish = subparsers.add_parser("publish")
    add_sources(publish)
    publish.add_argument("--output-parent", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--capsule-root", required=True)
    validate.add_argument("--source-bound", action="store_true")
    add_sources(validate)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "candidate":
            artifacts = build_capsule_artifacts(
                repo_root=arguments.repo_root,
                registry_root=arguments.registry_root,
                source_base=arguments.source_base,
            )
            output = _cli_summary(artifacts)
        elif arguments.command == "verify":
            artifacts = build_verified_capsule_artifacts(
                repo_root=arguments.repo_root,
                registry_root=arguments.registry_root,
                source_base=arguments.source_base,
            )
            output = _cli_summary(artifacts)
        elif arguments.command == "publish":
            artifacts = build_verified_capsule_artifacts(
                repo_root=arguments.repo_root,
                registry_root=arguments.registry_root,
                source_base=arguments.source_base,
            )
            manifest_sha256 = sha256_bytes(artifacts["file_payloads"]["capsule_manifest.json"])
            target = Path(arguments.output_parent).resolve(strict=True) / manifest_sha256
            output = write_capsule_artifacts(
                artifacts,
                target,
                repo_root=arguments.repo_root,
                registry_root=arguments.registry_root,
                source_base=arguments.source_base,
            )
        else:
            source_arguments = (
                {
                    "repo_root": arguments.repo_root,
                    "registry_root": arguments.registry_root,
                    "source_base": arguments.source_base,
                }
                if arguments.source_bound
                else {}
            )
            output = validate_capsule_directory(arguments.capsule_root, **source_arguments)
    except ReplayCapsuleError as error:
        failure = {
            "valid": False,
            "reason_code": error.code,
            "stage": _normalized_exclusion_stage(error.stage),
            "affected_json_pointer": (_stable_error_pointer(error)),
            "provider_invoked": False,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
            "execution_ready": False,
            "gpu_used": False,
            "gui_action_executed": False,
        }
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0
