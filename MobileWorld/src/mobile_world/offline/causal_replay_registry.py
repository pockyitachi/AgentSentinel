"""Freeze outcome-blind G1 causal-replay cases from audited MHR evidence.

This module is an offline derived-data consumer.  It never reads outcome
sidecars, failure-link artifacts, or treatment responses.  Strict-MHR case
selection does not use harm, task outcome, raw target-post, or later evidence,
and none of that evidence is projected into intervention or curator channels.
The separate clean-control pool may use the already frozen local
``NO_VISIBLE_HARM`` review label; its source lineage can therefore include a
natural target post-state.  This module creates a pre-gold registry only: every causal case is
``CANDIDATE_FROZEN`` and no case is executable until an independently curated,
hash-resolved gold/transformation bundle is attached in G1.6.
"""

from __future__ import annotations

import argparse
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
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, RefResolver

from mobile_world.offline.motivation_review import (
    canonical_json_bytes,
    canonical_sha256,
    load_canonical_json_line,
    validate_task_cards,
)
from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.serializer import ArtifactSerializer
from mobile_world.runtime.audit.serializer import canonical_json_bytes as sdk_json

PROTOCOL_VERSION = "mobileworld.g1.causal-replay/protocol-v1"
BUILDER_VERSION = "mobileworld.g1.causal-replay-registry-builder/v1"
CASE_SCHEMA_VERSION = "mobileworld.g1.causal-replay-case/v1"
LEDGER_SCHEMA_VERSION = "mobileworld.g1.causal-replay-selection-ledger/v1"
ARM_SCHEMA_VERSION = "mobileworld.g1.causal-replay-arm-catalog/v1"
MANIFEST_SCHEMA_VERSION = "mobileworld.g1.causal-replay-registry-manifest/v1"
VALIDATION_SCHEMA_VERSION = "mobileworld.g1.causal-replay-validation/v1"
MODEL_CONFIG_SCHEMA_VERSION = "mobileworld.g1.causal-replay/model-config-v1"
MODEL_CONFIG_MANIFEST_SHA256 = "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f"

CASE_STATUS = "CANDIDATE_FROZEN"
_STRICT_VALIDITY = frozenset({"REFUTED", "STALE"})
_STRICT_PROVENANCE = frozenset({"EXACT", "HIGH"})
_LOW_CONFOUND = frozenset({"NONE", "CURRENT_GUI_CONTRADICTS_PREMISE"})
_ALLOWED_HISTORY = frozenset({"FLAT_PROGRESS", "RAW_REPLAY"})
_ALLOWED_ROLES = frozenset({"PRIMARY", "REPLICATION"})
_ALLOWED_MAPPING = {
    "FLAT_PROGRESS": "exact_qwen_flat_progress",
    "RAW_REPLAY": "exact_content_monotonic",
}
_EXPECTED_LIVE_SOURCES = frozenset(
    {
        ("PRIMARY", "FLAT_PROGRESS", "qwen3vl_8b"),
        ("REPLICATION", "RAW_REPLAY", "mai_ui_8b"),
    }
)
_MODEL_CONFIG_RECORD_SHA256 = {
    "qwen3vl_8b": "5d6c5c1aa99aa13e8e153a19e6a0b1e8593cf2c32adaf8bca308fc76cea827e3",
    "mai_ui_8b": "c633cc272bca6e14ae788d90417c909d515fe1f22abb9c0bf5e02da78c8d7682",
}
_EXPECTED_CLEAN_SELECTION = {
    "qwen3vl_8b": (30, 30),
    "mai_ui_8b": (8, 7),
}
ARM_ORDER_SALT = "mobileworld-g1-arm-order-v1-20260826"
ARM_IDS = ("ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT")
LEDGER_REASON_CODES = frozenset(
    {
        "STRICT_MHR_CENSUS",
        "CLEAN_CONTROL_SELECTED",
        "CLEAN_CONTROL_RESERVE",
        "SOURCE_REFERENCE_UNRESOLVED",
        "PROVENANCE_BELOW_HIGH",
        "NOT_REFUTED_OR_STALE",
        "NO_EXPLICIT_UPTAKE",
        "NOT_STRICT_MHR",
    }
)
EXCLUSION_REASON_CODES = frozenset(
    {
        "SOURCE_REFERENCE_UNRESOLVED",
        "REQUEST_HASH_MISMATCH",
        "STATE_HASH_MISMATCH",
        "TARGET_SPAN_UNRESOLVED",
        "PROVENANCE_BELOW_HIGH",
        "NOT_REFUTED_OR_STALE",
        "NO_EXPLICIT_UPTAKE",
        "NOT_STRICT_MHR",
        "ORIGINAL_ACTION_UNPARSEABLE",
        "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING",
        "FUTURE_EVIDENCE_LEAKAGE",
        "NO_GOLD_CONSENSUS",
        "NO_VALID_CORRECTION",
        "NO_VALID_ORACLE_VIEW",
        "NO_MATCHED_SHAM",
        "ARM_PROTOCOL_INVALID",
        "DUPLICATE_CAPSULE",
    }
)
_CURATOR_EXCLUSION_CHANNEL_BY_REASON = {
    "NO_GOLD_CONSENSUS": "ACTION_GOLD",
    "NO_VALID_CORRECTION": "TRANSFORMATION",
    "NO_VALID_ORACLE_VIEW": "TRANSFORMATION",
    "NO_MATCHED_SHAM": "TRANSFORMATION",
}
_STRICT_ONLY_EXCLUSION_REASONS = frozenset(
    {
        "PROVENANCE_BELOW_HIGH",
        "NOT_REFUTED_OR_STALE",
        "NO_EXPLICIT_UPTAKE",
        "NOT_STRICT_MHR",
    }
)
_MECHANICAL_EXCLUSION_VALIDATOR_BY_REASON = {
    "SOURCE_REFERENCE_UNRESOLVED": "SOURCE_REGISTRY_RECORD_VALIDATOR",
    "REQUEST_HASH_MISMATCH": "REQUEST_VIEW_HASH_VALIDATOR",
    "STATE_HASH_MISMATCH": "CURRENT_GUI_HASH_VALIDATOR",
    "TARGET_SPAN_UNRESOLVED": "TARGET_SPAN_VALIDATOR",
    "PROVENANCE_BELOW_HIGH": "STRICT_MHR_GATE_VALIDATOR",
    "NOT_REFUTED_OR_STALE": "STRICT_MHR_GATE_VALIDATOR",
    "NO_EXPLICIT_UPTAKE": "STRICT_MHR_GATE_VALIDATOR",
    "NOT_STRICT_MHR": "STRICT_MHR_GATE_VALIDATOR",
    "ORIGINAL_ACTION_UNPARSEABLE": "ORIGINAL_ACTION_VALIDATOR",
    "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING": "BACKEND_CHECKPOINT_VALIDATOR",
    "FUTURE_EVIDENCE_LEAKAGE": "CURATION_FUTURE_EVIDENCE_VALIDATOR",
    "ARM_PROTOCOL_INVALID": "ARM_PROTOCOL_VALIDATOR",
    "DUPLICATE_CAPSULE": "DUPLICATE_CAPSULE_VALIDATOR",
}
_FUTURE_EVIDENCE_FAILURE_CODES = frozenset(
    {
        "admission_future_evidence_projection_forbidden",
        "curation_evidence_after_request_cutoff",
        "curation_evidence_after_target_step",
        "curation_evidence_projection_mismatch",
        "curation_evidence_role_forbidden",
        "transformation_evidence_not_prior_step",
    }
)
_FORBIDDEN_ACTION_TYPES = frozenset({"unknown", "error_env"})
_TEXT_PREDICATE_FIELD_BY_ACTION_TYPE = {
    "input_text": "text",
    "answer": "text",
    "finished": "text",
    "ask_user": "text",
    "open_app": "app_name",
    "status": "goal_status",
}
_STRICT_TRANSFORMATION_INVARIANTS = (
    "oracle_is_focal_superset",
    "mask_targets_equal_focal",
    "mask_correction_targets_equal_focal",
    "oracle_targets_equal_oracle_set",
)
_DELIMITER_REPAIR_PATTERNS = {
    "DELETE_EMPTY_DELIMITER": (
        re.compile(r"<thinking>\s*"),
        re.compile(r"\s*</thinking>"),
    ),
    "DELETE_ORPHAN_SEPARATOR": (
        re.compile(r"(?:Step\s+[1-9]\d*|Thought)\s*:\s*"),
        re.compile(r"\s*;\s*"),
    ),
}
_PRE_GOLD_MODEL_ASSIGNMENTS = {
    "qwen3vl_8b": (
        "PRIMARY",
        "FLAT_PROGRESS",
        "G1_1_FROZEN",
        "flat_progress",
        "conclusion_span_offsets_plus_enclosing_step_span",
    ),
    "mai_ui_8b": (
        "REPLICATION",
        "RAW_REPLAY",
        "G1_6_PENDING",
        "raw_replay",
        "raw_record_hash_with_g1_6_pending_edit_span",
    ),
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_QWEN_PATH_RE = re.compile(
    r"^payload\.request_view\.messages\[(?P<message>\d+)\]\.content"
    r"\[(?P<block>\d+)\]\.text\[(?P<start>\d+):(?P<end>\d+)\]$"
)
_QWEN_RECORD_PATH_RE = re.compile(
    r"^payload\.request_view\.messages\[(?P<message>\d+)\]\.content"
    r"\[(?P<block>\d+)\]\.text$"
)
_RAW_PATH_RE = re.compile(r"^payload\.request_view\.messages\[(?P<message>\d+)\]\.content$")
_QWEN_SEMANTIC_RECORD_RE = re.compile(
    r"Step\s+\d+\s*:.*?;[ \t]*(?=Step\s+\d+\s*:|\r?\n|$)",
    flags=re.DOTALL,
)
_FORBIDDEN_INPUT_TOKENS = (
    "outcome",
    "failure_link",
    "failure-link",
    "treatment_response",
    "treatment-response",
)
_FORBIDDEN_GOLD_ROLES = frozenset(
    {"target_prediction", "target_action", "target_post", "task_ended", "outcome"}
)
_ACTION_GOLD_FORBIDDEN = frozenset(
    {
        "history",
        "source_pre",
        "source_prediction",
        "source_action",
        "source_result",
        "source_post",
        "target_request_history",
        "target_prediction",
        "target_action",
        "target_post",
        "later_step",
        "task_ended",
        "outcome",
    }
)
_TRANSFORMATION_FORBIDDEN = frozenset(
    {"target_prediction", "target_action", "target_post", "later_step", "task_ended", "outcome"}
)
_FORBIDDEN_PROJECTED_FUTURE_KEYS = frozenset(
    {
        "downstream_effects",
        "evaluator",
        "later_step",
        "later_steps",
        "outcome",
        "target_post",
        "task_ended",
        "treatment_response",
        "treatment_responses",
    }
)


class CausalReplayRegistryError(RuntimeError):
    """A frozen input or requested registry violates the G1.1 contract."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


@dataclass(frozen=True, slots=True)
class RegistrySource:
    """One explicitly pinned, outcome-blind audit source."""

    source_key: str
    study_role: str
    history_family: str
    model_id: str
    audit_root: str
    final_reviews_relative_path: str
    final_reviews_sha256: str
    curated_manifest: str
    curated_manifest_sha256: str
    model_manifest: str
    model_manifest_sha256: str
    expected_task_count: int = 117
    expected_strict_case_count: int = 0
    expected_strict_task_count: int = 0
    expected_clean_pool_count: int = 0
    clean_control_target: int = 0
    clean_control_min_tasks: int = 0
    contract_files: tuple[tuple[str, str], ...] = ()
    source_config_path: str = ""
    source_config_sha256: str = ""
    source_config_byte_count: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> RegistrySource:
        _exact_keys(
            value,
            {
                "source_key",
                "study_role",
                "history_family",
                "model_id",
                "audit_root",
                "final_reviews_relative_path",
                "final_reviews_sha256",
                "curated_manifest",
                "curated_manifest_sha256",
                "model_manifest",
                "model_manifest_sha256",
                "expected_task_count",
                "expected_strict_case_count",
                "expected_strict_task_count",
                "expected_clean_pool_count",
                "clean_control_target",
                "clean_control_min_tasks",
            },
            path=path,
        )
        source = cls(
            source_key=_string(value["source_key"], f"{path}.source_key"),
            study_role=_string(value["study_role"], f"{path}.study_role"),
            history_family=_string(value["history_family"], f"{path}.history_family"),
            model_id=_string(value["model_id"], f"{path}.model_id"),
            audit_root=_string(value["audit_root"], f"{path}.audit_root"),
            final_reviews_relative_path=_relative_path(
                value["final_reviews_relative_path"], f"{path}.final_reviews_relative_path"
            ),
            final_reviews_sha256=_sha(
                value["final_reviews_sha256"], f"{path}.final_reviews_sha256"
            ),
            curated_manifest=_string(value["curated_manifest"], f"{path}.curated_manifest"),
            curated_manifest_sha256=_sha(
                value["curated_manifest_sha256"], f"{path}.curated_manifest_sha256"
            ),
            model_manifest=_string(value["model_manifest"], f"{path}.model_manifest"),
            model_manifest_sha256=_sha(
                value["model_manifest_sha256"], f"{path}.model_manifest_sha256"
            ),
            expected_task_count=_positive_int(
                value["expected_task_count"], f"{path}.expected_task_count"
            ),
            expected_strict_case_count=_nonnegative_int(
                value["expected_strict_case_count"], f"{path}.expected_strict_case_count"
            ),
            expected_strict_task_count=_nonnegative_int(
                value["expected_strict_task_count"], f"{path}.expected_strict_task_count"
            ),
            expected_clean_pool_count=_nonnegative_int(
                value["expected_clean_pool_count"], f"{path}.expected_clean_pool_count"
            ),
            clean_control_target=_nonnegative_int(
                value["clean_control_target"], f"{path}.clean_control_target"
            ),
            clean_control_min_tasks=_nonnegative_int(
                value["clean_control_min_tasks"], f"{path}.clean_control_min_tasks"
            ),
        )
        _require(source.study_role in _ALLOWED_ROLES, "study_role_invalid", path=path)
        _require(source.history_family in _ALLOWED_HISTORY, "history_family_invalid", path=path)
        _require(
            (source.study_role, source.history_family, source.model_id) in _EXPECTED_LIVE_SOURCES,
            "source_model_history_assignment_invalid",
            path=path,
        )
        _require(
            source.clean_control_min_tasks <= source.clean_control_target,
            "clean_control_min_tasks_invalid",
            path=path,
        )
        _require(
            (source.clean_control_target, source.clean_control_min_tasks)
            == _EXPECTED_CLEAN_SELECTION[source.model_id],
            "clean_control_selection_contract_drift",
            path=path,
        )
        _reject_forbidden_input_name(source.audit_root, path=f"{path}.audit_root")
        _reject_forbidden_input_name(
            source.final_reviews_relative_path,
            path=f"{path}.final_reviews_relative_path",
        )
        _reject_forbidden_input_name(source.curated_manifest, path=f"{path}.curated_manifest")
        _reject_forbidden_input_name(source.model_manifest, path=f"{path}.model_manifest")
        return source


def load_source_configuration(path: str | os.PathLike[str]) -> tuple[RegistrySource, ...]:
    """Load a strict, pinned source list; unknown keys and forbidden inputs fail."""

    supplied_config_path = Path(path)
    _require(not supplied_config_path.is_symlink(), "source_config_symlink")
    config_path = supplied_config_path.resolve(strict=True)
    config_bytes = _read_regular(config_path)
    payload = _load_json_bytes(config_bytes, config_path)
    _require(isinstance(payload, dict), "source_config_not_object")
    _exact_keys(
        payload,
        {"protocol_version", "curated", "deployment_prediction", "contract_files", "sources"},
        path="config",
    )
    _require(
        payload["protocol_version"] == PROTOCOL_VERSION,
        "protocol_version_mismatch",
        path="config.protocol_version",
    )
    _require(
        payload["deployment_prediction"] is False,
        "deployment_prediction_must_be_false",
        path="config.deployment_prediction",
    )
    _require(payload["curated"] is True, "curated_must_be_true", path="config.curated")
    contract_files = _validate_contract_files(payload["contract_files"])
    values = payload["sources"]
    _require(isinstance(values, list) and values, "sources_invalid", path="config.sources")
    parsed_sources = tuple(
        RegistrySource.from_mapping(value, path=f"config.sources[{index}]")
        for index, value in enumerate(values)
    )
    sources = tuple(
        replace(
            source,
            contract_files=contract_files,
            source_config_path=str(config_path),
            source_config_sha256=_digest(config_bytes),
            source_config_byte_count=len(config_bytes),
        )
        for source in parsed_sources
    )
    keys = [source.source_key for source in sources]
    _require(len(keys) == len(set(keys)), "source_key_duplicate")
    return tuple(sorted(sources, key=lambda source: source.source_key))


def _validate_contract_files(value: Any) -> tuple[tuple[str, str], ...]:
    _require(isinstance(value, list) and value, "contract_files_invalid")
    records: list[tuple[str, str]] = []
    for index, record in enumerate(value):
        path = f"config.contract_files[{index}]"
        _exact_keys(record, {"path", "sha256"}, path=path)
        relative = _relative_path(record["path"], f"{path}.path")
        digest = _sha(record["sha256"], f"{path}.sha256")
        records.append((relative, digest))
    _require(records == sorted(records), "contract_files_not_sorted")
    _require(len(records) == len({path for path, _ in records}), "contract_file_duplicate")
    repo_root = Path(__file__).resolve().parents[4]
    schema_paths = {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "mobileworld_audit_handoff" / "schemas" / "g1").glob(
            "*.schema.json"
        )
    }
    required = {
        "mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md",
        "mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md",
        "mobileworld_audit_handoff/g1/model_config_manifest.v1.json",
        "MobileWorld/src/mobile_world/offline/causal_replay_registry.py",
        "MobileWorld/scripts/build_g1_causal_replay_registry.py",
        "MobileWorld/tests/offline/test_causal_replay_registry.py",
        *schema_paths,
    }
    _require({path for path, _ in records} == required, "contract_file_set_drift")
    for relative, expected in records:
        data = _read_regular(_safe_child(repo_root, relative))
        _require(_digest(data) == expected, "contract_file_hash_drift", path=relative)
    return tuple(records)


def build_registry_artifacts(
    *, source_base: str | os.PathLike[str], sources: Sequence[RegistrySource]
) -> dict[str, Any]:
    """Build deterministic pre-gold cases, clean controls, ledger, and catalog."""

    base = _resolve_base(source_base)
    normalized = tuple(sorted(sources, key=lambda source: source.source_key))
    _require(normalized, "sources_empty")
    _require(
        len({source.source_key for source in normalized}) == len(normalized),
        "source_key_duplicate",
    )
    contract_files = normalized[0].contract_files
    source_config_identity = (
        normalized[0].source_config_path,
        normalized[0].source_config_sha256,
        normalized[0].source_config_byte_count,
    )
    _require(bool(contract_files), "contract_files_missing")
    _require(
        all(source.contract_files == contract_files for source in normalized),
        "contract_files_inconsistent",
    )
    _require(
        all(
            (
                source.source_config_path,
                source.source_config_sha256,
                source.source_config_byte_count,
            )
            == source_config_identity
            for source in normalized
        ),
        "source_config_identity_inconsistent",
    )

    cases: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    per_source_counts: dict[str, dict[str, int]] = {}
    model_manifests: dict[tuple[str, str], tuple[bytes, Mapping[str, Any]]] = {}

    for source in normalized:
        _require(
            source.model_manifest_sha256 == MODEL_CONFIG_MANIFEST_SHA256,
            "model_manifest_not_frozen_version",
            source_key=source.source_key,
        )
        cache_key = (source.model_manifest, source.model_manifest_sha256)
        if cache_key not in model_manifests:
            model_manifest_path = _resolve_pinned_file(source.model_manifest)
            model_bytes = _read_regular(model_manifest_path)
            _require(_digest(model_bytes) == source.model_manifest_sha256, "model_manifest_drift")
            model_manifest = _load_json_bytes(model_bytes, model_manifest_path)
            _require(isinstance(model_manifest, Mapping), "model_manifest_invalid")
            _validate_model_manifest(base, model_manifest, normalized)
            model_manifests[cache_key] = (model_bytes, model_manifest)

    for source in normalized:
        built = _build_source(
            base,
            source,
            model_manifest=model_manifests[(source.model_manifest, source.model_manifest_sha256)],
        )
        cases.extend(built["cases"])
        controls.extend(built["controls"])
        ledger.extend(built["ledger"])
        inputs.extend(built["inputs"])
        per_source_counts[source.source_key] = built["counts"]

    contract_inputs = [
        {
            "source_key": "__registry_contract__",
            "input_id": relative,
            "sha256": digest,
            "byte_count": _safe_child(Path(__file__).resolve().parents[4], relative).stat().st_size,
        }
        for relative, digest in contract_files
    ]
    contract_inputs.append(
        {
            "source_key": "__registry_contract__",
            "input_id": "source_registry_inputs.v1.json",
            "sha256": source_config_identity[1],
            "byte_count": source_config_identity[2],
        }
    )
    inputs.extend(contract_inputs)
    contract_aggregate_sha256 = canonical_sha256(
        [
            {
                "path": record["input_id"],
                "sha256": record["sha256"],
                "byte_count": record["byte_count"],
            }
            for record in contract_inputs
        ]
    )

    cases.sort(key=lambda record: record["case_id"])
    controls.sort(key=lambda record: record["control_id"])
    ledger.sort(key=lambda record: record["ledger_id"])
    _require(
        len({record["case_id"] for record in cases}) == len(cases),
        "case_id_collision",
    )
    _require(
        len(
            {
                (
                    record["source_key"],
                    record["task"]["task_run_id"],
                    record["decision"]["target_step"],
                )
                for record in cases
            }
        )
        == len(cases),
        "target_decision_duplicate",
    )
    _require(
        all(record["case_status"] == CASE_STATUS for record in cases),
        "case_status_not_pre_gold",
    )

    arm_catalog = _arm_catalog()
    validation = _validate_prepared(cases, controls, ledger, arm_catalog)
    file_payloads = {
        "case_registry.pre_gold.jsonl": _jsonl_bytes(cases),
        "clean_control_pool.jsonl": _jsonl_bytes(controls),
        "selection_ledger.jsonl": _jsonl_bytes(ledger),
        "arm_catalog.json": canonical_json_bytes(arm_catalog),
        "dry_validation.json": canonical_json_bytes(validation),
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "record_type": "causal_replay_registry_manifest",
        "protocol_version": PROTOCOL_VERSION,
        "builder_version": BUILDER_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "registry_phase": "PRE_GOLD_G1_1",
        "readiness": {
            "curation_and_admission_sealed": False,
            "admission_ready": False,
            "execution_ready": False,
            "run_ready": False,
            "treatment_response_generation_allowed": False,
        },
        "case_state_policy": {
            "source_registry_state": CASE_STATUS,
            "append_only_admission_states": ["INCLUDED", "EXCLUDED"],
            "included_count": 0,
            "g1_6_required_before_inclusion": True,
            "g1_6_rewrites_source_records": False,
        },
        "source_base": {
            "kind": "explicit_external_source_base",
            "absolute_path_is_content_identity": False,
        },
        "registry_contract": {
            "contract_file_count": len(contract_files),
            "source_config_sha256": source_config_identity[1],
            "aggregate_sha256": contract_aggregate_sha256,
        },
        "forbidden_inputs": [
            "outcome sidecars",
            "failure-link artifacts",
            "raw target-post or later evidence as strict-MHR intervention eligibility",
            "raw target-post or later evidence in action-gold or transformation curator channels",
            "task-ended events",
            "treatment responses",
        ],
        "inputs": sorted(inputs, key=lambda item: (item["source_key"], item["input_id"])),
        "counts": {
            "strict_mhr_case_count": len(cases),
            "strict_mhr_task_count": len(
                {(r["source_key"], r["task"]["task_name"]) for r in cases}
            ),
            "clean_control_selected_count": sum(
                record["control_status"] == "SELECTED" for record in controls
            ),
            "clean_control_reserve_count": sum(
                record["control_status"] == "RESERVE" for record in controls
            ),
            "ledger_count": len(ledger),
            "included_count": 0,
            "treatment_response_count": 0,
            "per_source": per_source_counts,
        },
        "files": {name: _file_summary(data, name) for name, data in sorted(file_payloads.items())},
    }
    file_payloads["registry_manifest.json"] = canonical_json_bytes(manifest)
    artifacts = {
        "cases": cases,
        "clean_controls": controls,
        "ledger": ledger,
        "arm_catalog": arm_catalog,
        "manifest": manifest,
        "validation": validation,
        "file_payloads": file_payloads,
    }
    _validate_emitted_schemas(artifacts)
    return artifacts


def write_registry_artifacts(
    artifacts: Mapping[str, Any], output_dir: str | os.PathLike[str]
) -> dict[str, Any]:
    """Install one immutable registry directory without overwriting anything."""

    destination = Path(output_dir)
    _require(not destination.exists(), "output_exists", path=str(destination))
    parent = destination.parent.resolve(strict=True)
    _require(not destination.is_symlink(), "output_symlink", path=str(destination))
    resolved_destination = parent / destination.name
    repo_root = Path(__file__).resolve().parents[4]
    _require(
        not _is_within(resolved_destination, repo_root),
        "repo_local_registry_output_forbidden",
        path=str(resolved_destination),
    )
    payloads = artifacts.get("file_payloads")
    _require(isinstance(payloads, Mapping) and payloads, "file_payloads_invalid")
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    destination_created = False
    destination_identity: tuple[int, int] | None = None
    destination_fd: int | None = None
    linked_paths: list[tuple[str, int, int]] = []

    def destination_is_owned() -> bool:
        if destination_identity is None:
            return False
        try:
            current = destination.lstat()
        except FileNotFoundError:
            return False
        return (
            stat.S_ISDIR(current.st_mode)
            and not destination.is_symlink()
            and (current.st_dev, current.st_ino) == destination_identity
        )

    try:
        for name, data in sorted(payloads.items()):
            _require(isinstance(name, str) and "/" not in name, "output_name_invalid")
            _require(isinstance(data, bytes), "output_bytes_invalid", file=name)
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        os.mkdir(destination, mode=0o700)
        destination_created = True
        _require(
            hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"),
            "secure_directory_open_flags_unavailable",
        )
        destination_fd = os.open(
            destination,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        destination_stat = os.fstat(destination_fd)
        _require(
            stat.S_ISDIR(destination_stat.st_mode) and not destination.is_symlink(),
            "output_directory_identity_invalid",
        )
        destination_identity = (destination_stat.st_dev, destination_stat.st_ino)
        for source_path in sorted(temporary.iterdir(), key=lambda path: path.name):
            _require(destination_is_owned(), "output_directory_replaced")
            linked_name = source_path.name
            os.link(source_path, linked_name, dst_dir_fd=destination_fd)
            linked_stat = os.stat(
                linked_name,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            linked_paths.append((linked_name, linked_stat.st_dev, linked_stat.st_ino))
        _require(destination_is_owned(), "output_directory_replaced")
        os.fchmod(destination_fd, 0o555)
        _require(destination_is_owned(), "output_directory_replaced")
    except Exception:
        if destination_created and destination_fd is not None:
            try:
                os.fchmod(destination_fd, 0o700)
            except OSError:
                # Continue with inode-checked cleanup; preserve the original failure.
                pass
            for linked_name, expected_device, expected_inode in reversed(linked_paths):
                try:
                    current = os.stat(
                        linked_name,
                        dir_fd=destination_fd,
                        follow_symlinks=False,
                    )
                    if current.st_dev == expected_device and current.st_ino == expected_inode:
                        os.unlink(linked_name, dir_fd=destination_fd)
                except OSError:
                    # Never replace the publication failure with cleanup failure.
                    pass
            if destination_is_owned():
                try:
                    destination.rmdir()
                except OSError:
                    # Preserve any path not installed by this invocation for investigation.
                    pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        shutil.rmtree(temporary, ignore_errors=True)
    return artifacts["manifest"]


def validate_registry_directory(
    registry_dir: str | os.PathLike[str],
    *,
    source_base: str | os.PathLike[str] | None = None,
    sources: Sequence[RegistrySource] | None = None,
) -> dict[str, Any]:
    """Validate canonical bytes, file hashes, invariants, and optional source drift."""

    supplied_root = Path(registry_dir)
    _require(not supplied_root.is_symlink(), "registry_root_symlink")
    root = supplied_root.resolve(strict=True)
    _require(root.is_dir() and not root.is_symlink(), "registry_root_invalid")
    manifest = _load_canonical_object(root / "registry_manifest.json")
    _require(manifest.get("deployment_prediction") is False, "deployment_prediction_must_be_false")
    _require(manifest.get("curated") is True, "curated_must_be_true")
    files = manifest.get("files")
    _require(isinstance(files, Mapping), "manifest_files_invalid")
    expected_file_names = {"registry_manifest.json", *files.keys()}
    _validate_registry_root_file_set(root, expected_file_names)
    for name, summary in files.items():
        path = _safe_child(root, name)
        data = _read_regular(path)
        _verify_summary(data, summary, path=str(path))
    cases = _load_canonical_jsonl(root / "case_registry.pre_gold.jsonl")
    controls = _load_canonical_jsonl(root / "clean_control_pool.jsonl")
    ledger = _load_canonical_jsonl(root / "selection_ledger.jsonl")
    arms = _load_canonical_object(root / "arm_catalog.json")
    for case in cases:
        validate_case_record(case)
    validation = _validate_prepared(cases, controls, ledger, arms)
    if source_base is not None or sources is not None:
        _require(
            source_base is not None and sources is not None, "source_rebuild_arguments_incomplete"
        )
        rebuilt = build_registry_artifacts(source_base=source_base, sources=sources)
        for name, data in rebuilt["file_payloads"].items():
            actual = _read_regular(_safe_child(root, name))
            _require(actual == data, "registry_source_drift", file=name)
    _validate_registry_root_file_set(root, expected_file_names)
    return validation


def _validate_registry_root_file_set(root: Path, expected_names: set[Any]) -> None:
    _require(
        all(
            isinstance(name, str)
            and name
            and name not in {".", ".."}
            and "/" not in name
            and "\\" not in name
            for name in expected_names
        ),
        "registry_manifest_file_name_invalid",
    )
    entries = list(root.iterdir())
    actual_names = {entry.name for entry in entries}
    _require(
        actual_names == expected_names,
        "registry_root_file_set_mismatch",
        expected=sorted(expected_names),
        actual=sorted(actual_names),
    )
    for entry in entries:
        metadata = entry.lstat()
        _require(
            stat.S_ISREG(metadata.st_mode) and not entry.is_symlink(),
            "registry_root_entry_not_regular",
            path=str(entry),
        )


def _validate_model_manifest(
    source_base: Path,
    manifest: Mapping[str, Any],
    sources: Sequence[RegistrySource],
) -> None:
    """Fail closed on the frozen model/config/environment/source lock.

    The complete manifest byte hash is pinned by :class:`RegistrySource`, so
    exact top-level keys plus that digest also reject every unknown nested key.
    This routine additionally rehashes every file pin and cross-binds each raw
    source run to the curated manifest used by the registry.
    """

    _exact_keys(
        manifest,
        {
            "artifact_type",
            "schema_version",
            "protocol_id",
            "manifest_phase",
            "curated",
            "deployment_prediction",
            "run_ready",
            "treatment_response_generation_allowed",
            "scientific_scope",
            "captured_vs_formal_replay_contract",
            "formal_preflight",
            "repository",
            "analysis_environment",
            "runtime_boundary",
            "models",
            "formal_serving_environment",
            "source_corpora",
            "source_platform_pins",
            "unavailable_historical_exact_values",
            "run_readiness",
        },
        path="model_manifest",
    )
    _require(
        manifest.get("artifact_type") == "g1_model_configuration_manifest"
        and manifest.get("schema_version") == MODEL_CONFIG_SCHEMA_VERSION
        and manifest.get("protocol_id") == PROTOCOL_VERSION
        and manifest.get("manifest_phase") == "G1.1_FROZEN_PRE_RESPONSE",
        "model_manifest_contract_mismatch",
    )
    _require(manifest.get("curated") is True, "model_manifest_not_curated")
    _require(
        manifest.get("deployment_prediction") is False
        and manifest.get("run_ready") is False
        and manifest.get("treatment_response_generation_allowed") is False,
        "model_manifest_pre_response_boundary_invalid",
    )
    replay_contract = manifest.get("captured_vs_formal_replay_contract")
    _require(isinstance(replay_contract, Mapping), "replay_contract_invalid")
    _require(
        replay_contract.get("allowed_application_argument_delta_from_capture") == ["seed"]
        and replay_contract.get("provider_seed_values") == [1729, 2718, 31415]
        and replay_contract.get("repeats_per_seed_arm") == 2
        and replay_contract.get("fresh_invocation_per_repeat") is True
        and replay_contract.get("session_or_kv_state_carryover_allowed") is False,
        "replay_seed_or_isolation_contract_invalid",
    )
    retry = replay_contract.get("retry_contract")
    _require(
        isinstance(retry, Mapping)
        and retry.get("formal_sdk_max_retries") == 0
        and retry.get("explicit_replay_retries_after_first_attempt") == 2
        and retry.get("exhaustion_status") == "MISSING",
        "replay_retry_contract_invalid",
    )
    preflight = manifest.get("formal_preflight")
    _require(
        isinstance(preflight, Mapping)
        and preflight.get("seed_support", {}).get("support_claimed") is False
        and preflight.get("seed_support", {}).get("unseeded_substitution_allowed") is False
        and preflight.get("serving_image", {}).get("content_digest") is None,
        "formal_preflight_must_be_pending",
    )
    runtime = manifest.get("runtime_boundary")
    _require(
        isinstance(runtime, Mapping)
        and runtime.get("backend_dependency") == "NONE_FOR_SERIALIZED_MODEL_CALL"
        and runtime.get("backend_checkpoint_required") is False
        and runtime.get("backend_checkpoint_reference") is None
        and runtime.get("generated_action_execution_allowed") is False,
        "runtime_boundary_invalid",
    )
    run_readiness = manifest.get("run_readiness")
    _require(
        isinstance(run_readiness, Mapping)
        and run_readiness.get("run_ready") is False
        and run_readiness.get("included_count") == 0
        and run_readiness.get("treatment_response_generation_allowed") is False
        and isinstance(run_readiness.get("blockers"), list)
        and len(run_readiness["blockers"]) >= 9
        and run_readiness.get("failure_mode") == "FAIL_CLOSED_BEFORE_ANY_TREATMENT_RESPONSE",
        "model_manifest_run_readiness_invalid",
    )

    repo_root = Path(__file__).resolve().parents[4]
    hash_cache: dict[Path, tuple[int, str]] = {}

    def verify_pin(
        pin: Any,
        *,
        path: str,
        relative_root: Path | None = None,
        allow_hf_symlink: bool = False,
    ) -> Path:
        _require(isinstance(pin, Mapping), "model_manifest_file_pin_invalid", path=path)
        relative_or_absolute = _string(pin.get("path"), f"{path}.path")
        expected = _sha(pin.get("sha256"), f"{path}.sha256")
        supplied = Path(relative_or_absolute)
        if supplied.is_absolute():
            candidate = supplied
        else:
            _require(relative_root is not None, "model_manifest_relative_pin_root_missing")
            relative = _relative_path(relative_or_absolute, f"{path}.path")
            candidate = relative_root.joinpath(*PurePosixPath(relative).parts)
        _require(candidate.exists(), "model_manifest_pinned_file_missing", path=str(candidate))
        if allow_hf_symlink:
            _require(relative_root is not None, "hf_snapshot_root_missing")
            _reject_symlink_components(relative_root, candidate.parent.resolve(strict=True))
            resolved = candidate.resolve(strict=True)
            model_cache_root = relative_root.parents[1]
            _require(
                _is_within(resolved, model_cache_root),
                "hf_artifact_symlink_escape",
                path=str(candidate),
            )
        else:
            _require(not candidate.is_symlink(), "model_manifest_pin_symlink", path=str(candidate))
            resolved = candidate.resolve(strict=True)
            if relative_root is not None:
                _require(_is_within(resolved, relative_root), "model_manifest_pin_escape")
                _reject_symlink_components(relative_root, resolved)
        _require(resolved.is_file(), "model_manifest_pin_not_file", path=str(resolved))
        if resolved not in hash_cache:
            hash_cache[resolved] = (resolved.stat().st_size, _digest_file(resolved))
        actual_size, actual_digest = hash_cache[resolved]
        if "byte_count" in pin:
            _require(pin.get("byte_count") == actual_size, "model_manifest_pin_size_drift")
        _require(actual_digest == expected, "model_manifest_pin_hash_drift", path=str(candidate))
        return resolved

    repository = manifest.get("repository")
    analysis_env = manifest.get("analysis_environment")
    serving_env = manifest.get("formal_serving_environment")
    _require(
        isinstance(repository, Mapping)
        and isinstance(analysis_env, Mapping)
        and isinstance(serving_env, Mapping),
        "model_manifest_environment_invalid",
    )
    verify_pin(
        repository.get("mobileworld_pyproject"),
        path="repository.pyproject",
        relative_root=repo_root,
    )
    verify_pin(repository.get("mobileworld_lock"), path="repository.lock", relative_root=repo_root)
    for index, pin in enumerate(analysis_env.get("environment_identity", [])):
        verify_pin(
            pin, path=f"analysis_environment.environment_identity[{index}]", relative_root=repo_root
        )
    for index, pin in enumerate(serving_env.get("environment_identity", [])):
        verify_pin(pin, path=f"formal_serving_environment.environment_identity[{index}]")

    model_values = manifest.get("models")
    _require(
        isinstance(model_values, list) and len(model_values) == 2, "model_manifest_models_invalid"
    )
    models = {model.get("model_id"): model for model in model_values if isinstance(model, Mapping)}
    _require(set(models) == {"qwen3vl_8b", "mai_ui_8b"}, "model_manifest_model_set_invalid")
    expected_models = {
        "qwen3vl_8b": ("PRIMARY", "flat_progress", "Qwen3-VL-8B-Instruct"),
        "mai_ui_8b": ("REPLICATION", "raw_replay", "MAI-UI-8B"),
    }
    for model_id, (role, history, served_name) in expected_models.items():
        model = models[model_id]
        _require(
            model.get("role") == role
            and model.get("history_family") == history
            and model.get("served_model_name") == served_name,
            "model_manifest_assignment_invalid",
            model_id=model_id,
        )
        revision = _string(model.get("model_revision"), f"models.{model_id}.model_revision")
        snapshot = Path(_string(model.get("local_snapshot_reference"), "local_snapshot_reference"))
        _require(
            snapshot.is_absolute()
            and not snapshot.is_symlink()
            and snapshot.resolve(strict=True).is_dir()
            and snapshot.name == revision,
            "model_snapshot_revision_invalid",
            model_id=model_id,
        )
        checkpoint = model.get("checkpoint_artifacts")
        tokenizer = model.get("tokenizer")
        _require(
            isinstance(checkpoint, Mapping) and isinstance(tokenizer, Mapping),
            "model_artifacts_invalid",
        )
        _require(
            tokenizer.get("revision") == revision
            and tokenizer.get("transformers_version") == "4.57.4"
            and tokenizer.get("tokenizers_version") == "0.22.2"
            and tokenizer.get("use_fast") is True
            and tokenizer.get("trust_remote_code") is False,
            "tokenizer_contract_invalid",
            model_id=model_id,
        )
        for group in ("config_files", "weight_shards"):
            pins = checkpoint.get(group)
            _require(isinstance(pins, list) and pins, "checkpoint_pin_group_invalid")
            for index, pin in enumerate(pins):
                verify_pin(
                    pin,
                    path=f"models.{model_id}.checkpoint_artifacts.{group}[{index}]",
                    relative_root=snapshot,
                    allow_hf_symlink=True,
                )
        tokenizer_pins = tokenizer.get("artifacts")
        _require(isinstance(tokenizer_pins, list) and tokenizer_pins, "tokenizer_pins_invalid")
        for index, pin in enumerate(tokenizer_pins):
            verify_pin(
                pin,
                path=f"models.{model_id}.tokenizer.artifacts[{index}]",
                relative_root=snapshot,
                allow_hf_symlink=True,
            )
        repo_pins = [
            model.get("prompt"),
            model.get("actor_adapter"),
            model.get("parser_implementation"),
            model.get("normalized_action_schema"),
        ]
        parser = model.get("parser_implementation")
        _require(isinstance(parser, Mapping), "parser_implementation_invalid")
        if isinstance(parser.get("mapping"), Mapping):
            repo_pins.append(parser["mapping"])
        if isinstance(parser.get("helper"), Mapping):
            repo_pins.append(parser["helper"])
        for index, pin in enumerate(repo_pins):
            verify_pin(pin, path=f"models.{model_id}.repo_pin[{index}]", relative_root=repo_root)
        captured = model.get("captured_application_request")
        formal = model.get("formal_replay_request")
        _require(
            isinstance(captured, Mapping) and isinstance(formal, Mapping),
            "request_contract_invalid",
        )
        _require(
            captured.get("endpoint_origin") == formal.get("endpoint_origin")
            and captured.get("endpoint_path")
            == formal.get("endpoint_path")
            == "/v1/chat/completions"
            and captured.get("sdk") == formal.get("sdk") == "openai.chat.completions.create"
            and captured.get("provider_seed_present") is False
            and formal.get("seed_required") is True
            and formal.get("sdk_max_retries") == 0,
            "captured_formal_request_contract_invalid",
            model_id=model_id,
        )
        historical = model.get("historical_serving_evidence")
        if isinstance(historical, Mapping) and "log_reference" in historical:
            verify_pin(
                {
                    "path": historical.get("log_reference"),
                    "sha256": historical.get("log_sha256"),
                    "byte_count": historical.get("log_byte_count"),
                },
                path=f"models.{model_id}.historical_serving_evidence",
            )

    corpora_values = manifest.get("source_corpora")
    _require(
        isinstance(corpora_values, list) and len(corpora_values) == 2, "source_corpora_invalid"
    )
    corpora = {
        corpus.get("model_id"): corpus for corpus in corpora_values if isinstance(corpus, Mapping)
    }
    source_by_model = {source.model_id: source for source in sources}
    _require(set(corpora) == set(source_by_model) == set(models), "source_corpus_model_set_invalid")
    for model_id, source in source_by_model.items():
        corpus = corpora[model_id]
        _require(
            corpus.get("external_source_base") == str(source_base), "source_base_manifest_mismatch"
        )
        curated_pin = corpus.get("curated_manifest")
        _require(isinstance(curated_pin, Mapping), "curated_manifest_pin_invalid")
        curated_path = verify_pin(curated_pin, path=f"source_corpora.{model_id}.curated_manifest")
        _require(
            curated_path == _resolve_input(source_base, source.curated_manifest)
            and curated_pin.get("sha256") == source.curated_manifest_sha256,
            "source_config_curated_manifest_crossbind_failed",
            model_id=model_id,
        )
        curated = _load_json_object(curated_path)
        _require(
            curated.get("selection_sha256") == curated_pin.get("selection_sha256")
            and curated.get("canonical_catalog") == corpus.get("canonical_task_catalog"),
            "curated_selection_or_catalog_crossbind_failed",
            model_id=model_id,
        )
        curated_runs = {
            run.get("source_id"): run
            for run in curated.get("sources", [])
            if isinstance(run, Mapping)
        }
        source_runs = corpus.get("source_runs")
        _require(isinstance(source_runs, list) and source_runs, "source_runs_invalid")
        _require(
            {run.get("source_id") for run in source_runs} == set(curated_runs),
            "source_run_set_crossbind_failed",
            model_id=model_id,
        )
        for run in source_runs:
            source_id = run.get("source_id")
            curated_run = curated_runs[source_id]
            _require(
                run.get("raw_run_id") == curated_run.get("run_id")
                and run.get("relative_run_path") == curated_run.get("relative_run_path")
                and run.get("selected_task_count") == curated_run.get("selected_task_count")
                and run.get("manifest_start_sha256")
                == curated_run.get("manifest_start", {}).get("sha256")
                and run.get("manifest_final_sha256")
                == curated_run.get("manifest_final", {}).get("sha256")
                and run.get("captured_environment_image")
                == curated_run.get("provenance", {}).get("environment_image"),
                "raw_source_run_crossbind_failed",
                model_id=model_id,
                source_id=source_id,
            )
            run_root = _resolve_input(source_base, run.get("relative_run_path"))
            start_path = _safe_child(run_root, curated_run["manifest_start"]["relative_path"])
            final_path = _safe_child(run_root, curated_run["manifest_final"]["relative_path"])
            start_bytes = _read_regular(start_path)
            final_bytes = _read_regular(final_path)
            _require(
                _digest(start_bytes) == run.get("manifest_start_sha256"), "raw_start_manifest_drift"
            )
            _require(
                _digest(final_bytes) == run.get("manifest_final_sha256"), "raw_final_manifest_drift"
            )
            start = _load_json_bytes(start_bytes, start_path)
            final = _load_json_bytes(final_bytes, final_path)
            _require(
                isinstance(start, Mapping) and isinstance(final, Mapping), "raw_manifest_invalid"
            )
            model = models[model_id]
            _require(
                start.get("run_id") == run.get("raw_run_id")
                and final.get("run_id") == run.get("raw_run_id")
                and start.get("environment_image") == run.get("captured_environment_image")
                and start.get("model_name") == model.get("served_model_name"),
                "raw_manifest_identity_crossbind_failed",
            )
            cli = start.get("resolved_cli_config")
            task_parameters = run.get("task_parameters")
            _require(
                isinstance(cli, Mapping) and isinstance(task_parameters, Mapping),
                "task_parameters_invalid",
            )
            for field in (
                "task",
                "max_round",
                "scale_factor",
                "pass_k",
                "auto_retry",
                "max_concurrency",
                "shuffle_tasks",
            ):
                if field == "task" and source_id == "rerun15":
                    continue
                _require(
                    cli.get(field) == task_parameters.get(field),
                    "task_parameters_crossbind_failed",
                    field=field,
                    source_id=source_id,
                )


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_source(
    base: Path,
    source: RegistrySource,
    *,
    model_manifest: tuple[bytes, Mapping[str, Any]],
) -> dict[str, Any]:
    _require(
        (source.clean_control_target, source.clean_control_min_tasks)
        == _EXPECTED_CLEAN_SELECTION[source.model_id],
        "clean_control_selection_contract_drift",
        source_key=source.source_key,
    )
    audit_root = _resolve_input(base, source.audit_root)
    curated_path = _resolve_input(base, source.curated_manifest)
    cards_manifest_path = _safe_child(audit_root, "cards/manifest.json")
    cards_path = _safe_child(audit_root, "cards/task_cards.jsonl")
    recon_path = _safe_child(audit_root, "cards/reconstruction_refs.jsonl")
    reviews_path = _safe_child(audit_root, source.final_reviews_relative_path)

    curated_bytes = _read_regular(curated_path)
    _require(_digest(curated_bytes) == source.curated_manifest_sha256, "curated_manifest_drift")
    model_bytes, model_manifest_payload = model_manifest
    model_entries = model_manifest_payload.get("models")
    _require(isinstance(model_entries, list), "model_manifest_models_invalid")
    matching_models = [entry for entry in model_entries if entry.get("model_id") == source.model_id]
    _require(len(matching_models) == 1, "model_manifest_model_mismatch")
    model_entry = matching_models[0]
    model_config_record_sha256 = canonical_sha256(model_entry)
    _require(
        model_config_record_sha256 == _MODEL_CONFIG_RECORD_SHA256[source.model_id],
        "model_config_record_hash_drift",
    )
    _require(
        str(model_entry.get("history_family", "")).upper() == source.history_family,
        "model_manifest_history_mismatch",
    )
    _require(model_entry.get("role") == source.study_role, "model_manifest_role_mismatch")
    served_model_name = model_entry.get("served_model_name")
    _require(isinstance(served_model_name, str) and served_model_name, "served_model_name_missing")

    cards_manifest_bytes = _read_regular(cards_manifest_path)
    cards_manifest = _load_json_bytes(cards_manifest_bytes, cards_manifest_path)
    _require(isinstance(cards_manifest, Mapping), "cards_manifest_invalid")
    _require(
        cards_manifest.get("artifact_type") == "derived_outcome_blinded_review_bundle",
        "cards_not_outcome_blinded",
    )
    card_input = cards_manifest.get("input")
    _require(isinstance(card_input, Mapping), "cards_manifest_input_invalid")
    _require(
        card_input.get("curated_manifest_sha256") == source.curated_manifest_sha256,
        "cards_curated_manifest_mismatch",
    )
    summaries = cards_manifest.get("files")
    _require(isinstance(summaries, Mapping), "cards_file_summaries_invalid")
    cards_bytes = _read_regular(cards_path)
    recon_bytes = _read_regular(recon_path)
    _verify_summary(cards_bytes, summaries.get("task_cards.jsonl"), path=str(cards_path))
    _verify_summary(
        recon_bytes,
        summaries.get("reconstruction_refs.jsonl"),
        path=str(recon_path),
    )
    reviews_bytes = _read_regular(reviews_path)
    _require(_digest(reviews_bytes) == source.final_reviews_sha256, "final_reviews_drift")

    cards_list = _load_canonical_jsonl_bytes(cards_bytes, cards_path)
    cards_by_task = validate_task_cards(
        {card["task"]["task_name"]: card for card in cards_list},
        expected_task_count=source.expected_task_count,
    )
    reviews = _load_canonical_jsonl_bytes(reviews_bytes, reviews_path)
    recons = _load_canonical_jsonl_bytes(recon_bytes, recon_path)
    _require(len(reviews) == source.expected_task_count, "review_coverage_count")
    _require(len(recons) == source.expected_task_count, "reconstruction_coverage_count")
    reviews_by_task = _validate_reviews(cards_by_task, reviews)
    recons_by_task = _validate_reconstructions(cards_by_task, recons)

    curated = _load_json_bytes(curated_bytes, curated_path)
    _require(isinstance(curated, Mapping), "curated_manifest_invalid")
    for curated_source in curated.get("sources", []):
        provenance = curated_source.get("provenance", {})
        _require(provenance.get("model_name") == served_model_name, "curated_model_mismatch")
        expected_agent = "qwen3vl" if source.study_role == "PRIMARY" else "mai_ui_agent"
        _require(provenance.get("agent_type") == expected_agent, "curated_agent_mismatch")
    _verify_raw_manifests(base, curated)

    evaluated: list[dict[str, Any]] = []
    task_stream_cache: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for task_name in sorted(
        cards_by_task, key=lambda name: cards_by_task[name]["task"]["catalog_index"]
    ):
        card = cards_by_task[task_name]
        review = reviews_by_task[task_name]
        reconstruction = recons_by_task[task_name]
        chains = {chain["candidate_id"]: chain for chain in review["chains"]}
        for candidate in card["candidates"]:
            chain = chains[candidate["candidate_id"]]
            gates = _gate_facts(card, review, candidate, chain)
            strict = all(gates["strict_mhr"].values())
            clean = all(gates["clean_control"].values())
            evaluated.append(
                {
                    "task_name": task_name,
                    "card": card,
                    "review": review,
                    "reconstruction": reconstruction,
                    "candidate": candidate,
                    "chain": chain,
                    "gates": gates,
                    "strict": strict,
                    "clean": clean,
                    "rank": _stable_hash(
                        "candidate-rank",
                        source.source_key,
                        task_name,
                        candidate["candidate_id"],
                    ),
                }
            )

    clean_pool = [item for item in evaluated if item["clean"] and not item["strict"]]
    selected_clean = _select_clean_controls(
        clean_pool,
        target=source.clean_control_target,
        minimum_tasks=source.clean_control_min_tasks,
    )
    selected_ids = {
        (item["task_name"], item["candidate"]["candidate_id"]) for item in selected_clean
    }
    clean_decisions = [
        (
            item["card"]["task"]["task_run_id"],
            item["candidate"]["exposure"]["target_step"],
        )
        for item in clean_pool
    ]
    _require(
        len(clean_decisions) == len(set(clean_decisions)),
        "clean_control_decision_duplicate",
    )

    cases: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    strict_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in evaluated:
        if item["strict"]:
            strict_groups[(item["task_name"], item["candidate"]["exposure"]["target_step"])].append(
                item
            )
    for group_key in sorted(strict_groups):
        focal_items = sorted(
            strict_groups[group_key],
            key=lambda value: (
                value["candidate"]["exposure"]["request_path"],
                value["candidate"]["exposure"]["span_sha256"],
                value["candidate"]["candidate_id"],
            ),
        )
        cases.append(
            _materialize_case(
                base,
                source,
                {
                    **focal_items[0],
                    "served_model_name": served_model_name,
                    "focal_items": focal_items,
                },
                task_stream_cache,
                model_config_record_sha256=model_config_record_sha256,
            )
        )
    strict_unit_ids = {
        (record["task"]["task_name"], record["decision"]["target_step"]): record["case_id"]
        for record in cases
    }
    for item in evaluated:
        candidate_id = item["candidate"]["candidate_id"]
        unit_kind: str | None = None
        unit_id: str | None = None
        if item["strict"]:
            disposition = "CANDIDATE_FROZEN"
            reasons = ["STRICT_MHR_CENSUS"]
            unit_kind = "STRICT_MHR"
            unit_id = strict_unit_ids[
                (item["task_name"], item["candidate"]["exposure"]["target_step"])
            ]
        elif item["clean"]:
            selected = (item["task_name"], candidate_id) in selected_ids
            control = _materialize_control(
                base,
                source,
                {**item, "served_model_name": served_model_name},
                task_stream_cache,
                selected=selected,
                model_config_record_sha256=model_config_record_sha256,
            )
            controls.append(control)
            disposition = "CLEAN_CONTROL_SELECTED" if selected else "CLEAN_CONTROL_RESERVE"
            reasons = [disposition]
            unit_kind = "CLEAN_CONTROL"
            unit_id = control["control_id"]
        else:
            disposition = "EXCLUDED"
            reasons = _exclusion_reasons(item["gates"])
        ledger.append(
            _ledger_record(
                source,
                item,
                disposition,
                reasons,
                model_config_record_sha256=model_config_record_sha256,
                unit_kind=unit_kind,
                unit_id=unit_id,
            )
        )

    strict_tasks = {record["task"]["task_name"] for record in cases}
    selected_control_records = [r for r in controls if r["control_status"] == "SELECTED"]
    selected_control_tasks = {record["task"]["task_name"] for record in selected_control_records}
    _require(
        len(cases) == source.expected_strict_case_count,
        "strict_case_census_drift",
        expected=source.expected_strict_case_count,
        actual=len(cases),
    )
    _require(
        len(strict_tasks) == source.expected_strict_task_count,
        "strict_task_census_drift",
        expected=source.expected_strict_task_count,
        actual=len(strict_tasks),
    )
    _require(
        len(controls) == source.expected_clean_pool_count,
        "clean_control_pool_census_drift",
        expected=source.expected_clean_pool_count,
        actual=len(controls),
    )
    _require(
        len(selected_control_records) == source.clean_control_target,
        "clean_control_target_unmet",
        source_key=source.source_key,
    )
    _require(
        len(selected_control_tasks) >= source.clean_control_min_tasks,
        "clean_control_task_minimum_unmet",
        source_key=source.source_key,
    )
    inputs = [
        _input_record(source.source_key, "cards_manifest", cards_manifest_bytes),
        _input_record(source.source_key, "task_cards", cards_bytes),
        _input_record(source.source_key, "reconstruction_refs", recon_bytes),
        _input_record(source.source_key, "final_reviews", reviews_bytes),
        _input_record(source.source_key, "curated_manifest_identity_only", curated_bytes),
        _input_record(source.source_key, "model_config_manifest", model_bytes),
    ]
    return {
        "cases": cases,
        "controls": controls,
        "ledger": ledger,
        "inputs": inputs,
        "counts": {
            "strict_mhr_case_count": len(cases),
            "strict_mhr_task_count": len(strict_tasks),
            "clean_control_pool_count": len(controls),
            "clean_control_selected_count": len(selected_control_records),
            "clean_control_selected_task_count": len(selected_control_tasks),
            "ledger_count": len(ledger),
            "included_count": 0,
        },
    }


def _paired_unit_hash(
    identity_kind: str,
    *,
    model_config_manifest_sha256: str,
    model_config_record_sha256: str,
    task_run_id: str,
    target_step: int,
    request_event_id: str,
    request_view_sha256: str,
    current_gui_sha256: str,
    sdk_arguments_snapshot_sha256: str,
) -> str:
    return _stable_hash(
        identity_kind,
        PROTOCOL_VERSION,
        model_config_manifest_sha256,
        model_config_record_sha256,
        task_run_id,
        str(target_step),
        request_event_id,
        request_view_sha256,
        current_gui_sha256,
        sdk_arguments_snapshot_sha256,
    )


def _paired_unit_identity(
    identity_kind: str,
    *,
    source: RegistrySource,
    model_config_record_sha256: str,
    task_run_id: str,
    target_step: int,
    capsule: Mapping[str, Any],
) -> str:
    return _paired_unit_hash(
        identity_kind,
        model_config_manifest_sha256=source.model_manifest_sha256,
        model_config_record_sha256=model_config_record_sha256,
        task_run_id=task_run_id,
        target_step=target_step,
        request_event_id=capsule["decision"]["request_event_id"],
        request_view_sha256=capsule["request_view_sha256"],
        current_gui_sha256=capsule["current_gui_blob"]["digest"],
        sdk_arguments_snapshot_sha256=capsule["sdk_arguments_snapshot_blob"]["digest"],
    )


def _materialize_case(
    base: Path,
    source: RegistrySource,
    item: Mapping[str, Any],
    cache: dict[tuple[str, str, str], dict[str, dict[str, Any]]],
    *,
    model_config_record_sha256: str,
) -> dict[str, Any]:
    capsule = _decision_capsule(
        base,
        source,
        item,
        cache,
        model_config_record_sha256=model_config_record_sha256,
    )
    focal_items = item["focal_items"]
    resolved_spans = capsule["resolved_target_spans"]
    _require(len(resolved_spans) == len(focal_items), "focal_resolution_count_mismatch")
    audited_exposure_bindings = [span["target_set_entry"] for span in resolved_spans]
    identity = _paired_unit_identity(
        "strict-mhr-case",
        source=source,
        model_config_record_sha256=model_config_record_sha256,
        task_run_id=item["card"]["task"]["task_run_id"],
        target_step=item["candidate"]["exposure"]["target_step"],
        capsule=capsule,
    )
    task = capsule.pop("task")
    decision = capsule.pop("decision")
    resolved_by_candidate = {
        span["target_set_entry"]["candidate_id"]: span for span in resolved_spans
    }
    _require(
        len(resolved_by_candidate) == len(resolved_spans),
        "resolved_candidate_id_duplicate",
    )
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "record_type": "causal_replay_case",
        "protocol_version": PROTOCOL_VERSION,
        "case_id": f"g1case-{identity[:24]}",
        "case_status": CASE_STATUS,
        "case_kind": "STRICT_MHR",
        "curated": True,
        "deployment_prediction": False,
        "source_key": source.source_key,
        "study_role": source.study_role,
        "model_id": source.model_id,
        "model_config_manifest_sha256": source.model_manifest_sha256,
        "model_config_record_sha256": model_config_record_sha256,
        "history_family": source.history_family,
        "task": task,
        "decision": decision,
        "frozen_capsule": capsule,
        "target_histories": [
            _target_history(
                source,
                focal_item,
                resolved_by_candidate[focal_item["candidate"]["candidate_id"]],
            )
            for focal_item in focal_items
        ],
        "eligibility_only_refs": {
            "gate_version": "strict-mhr-exact-exposure/v1",
            "facts": item["gates"]["strict_mhr"],
            "review_id": item["review"]["review_id"],
            "review_sha256": canonical_sha256(item["review"]),
            "members": [
                {
                    "candidate_id": focal_item["candidate"]["candidate_id"],
                    "candidate_sha256": canonical_sha256(focal_item["candidate"]),
                    "history_validity": focal_item["chain"]["history_validity"],
                    "uptake_evidence": focal_item["chain"]["uptake_evidence"],
                    "state_confound": focal_item["chain"]["state_confound"],
                    "evidence_ref_ids": sorted(
                        ref["ref_id"]
                        for ref in focal_item["candidate"]["evidence_refs"]
                        if ref["role"] != "target_post"
                    ),
                }
                for focal_item in focal_items
            ],
        },
        "action_gold_refs": {
            "accepted_next_action_set_ref": None,
            "review_ledger_ref": None,
            "curator_identity_ref": None,
            "curator_view": "TASK_AND_PRE_CALL_GUI_ONLY",
            "allowed_evidence_roles": [
                "ask_user_response",
                "target_pre",
                "task_instruction",
                "tool_response",
            ],
            "forbidden_evidence_roles": sorted(_ACTION_GOLD_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "transformation_refs": {
            "transformation_plan_ref": None,
            "review_ledger_ref": None,
            "mask_correction_ref": None,
            "oracle_clean_ref": None,
            "sham_benign_edit_ref": None,
            "curator_identity_ref": None,
            "curator_view": "SOURCE_HISTORY_AND_PRE_CALL_GUI_NO_TARGET_DECISION",
            "audited_exposure_bindings": audited_exposure_bindings,
            "focal_target_set": [],
            "focal_target_set_status": "G1_6_PENDING",
            "oracle_target_set_ref": None,
            "forbidden_evidence_roles": sorted(_TRANSFORMATION_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "arm_eligibility": _arm_eligibility(case_kind="STRICT_MHR"),
    }


def _materialize_control(
    base: Path,
    source: RegistrySource,
    item: Mapping[str, Any],
    cache: dict[tuple[str, str, str], dict[str, dict[str, Any]]],
    *,
    selected: bool,
    model_config_record_sha256: str,
) -> dict[str, Any]:
    capsule = _decision_capsule(
        base,
        source,
        item,
        cache,
        model_config_record_sha256=model_config_record_sha256,
    )
    candidate = item["candidate"]
    resolved_span = capsule["resolved_target_spans"][0]
    audited_exposure_bindings = [resolved_span["target_set_entry"]]
    identity = _paired_unit_identity(
        "clean-control",
        source=source,
        model_config_record_sha256=model_config_record_sha256,
        task_run_id=item["card"]["task"]["task_run_id"],
        target_step=candidate["exposure"]["target_step"],
        capsule=capsule,
    )
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "record_type": "causal_replay_clean_control",
        "protocol_version": PROTOCOL_VERSION,
        "control_id": f"g1control-{identity[:24]}",
        "control_status": "SELECTED" if selected else "RESERVE",
        "curated": True,
        "deployment_prediction": False,
        "source_key": source.source_key,
        "study_role": source.study_role,
        "model_id": source.model_id,
        "model_config_manifest_sha256": source.model_manifest_sha256,
        "model_config_record_sha256": model_config_record_sha256,
        "history_family": source.history_family,
        "task": capsule.pop("task"),
        "decision": capsule.pop("decision"),
        "frozen_capsule": capsule,
        "target_histories": [_target_history(source, item, resolved_span)],
        "eligibility_only_refs": {
            "gate_version": "clean-supported-explicit-no-harm/v1",
            "facts": item["gates"]["clean_control"],
            "review_id": item["review"]["review_id"],
            "review_sha256": canonical_sha256(item["review"]),
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": canonical_sha256(candidate),
        },
        "action_gold_refs": {
            "accepted_next_action_set_ref": None,
            "review_ledger_ref": None,
            "curator_identity_ref": None,
            "curator_view": "TASK_AND_PRE_CALL_GUI_ONLY",
            "allowed_evidence_roles": [
                "ask_user_response",
                "target_pre",
                "task_instruction",
                "tool_response",
            ],
            "forbidden_evidence_roles": sorted(_ACTION_GOLD_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "transformation_refs": {
            "transformation_plan_ref": None,
            "review_ledger_ref": None,
            "sham_benign_edit_ref": None,
            "curator_identity_ref": None,
            "curator_view": "SOURCE_HISTORY_AND_PRE_CALL_GUI_NO_TARGET_DECISION",
            "audited_exposure_bindings": audited_exposure_bindings,
            "focal_target_set": [],
            "focal_target_set_status": "G1_6_PENDING",
            "oracle_target_set_ref": None,
            "forbidden_evidence_roles": sorted(_TRANSFORMATION_FORBIDDEN),
            "curation_phase": "G1_6_PENDING",
        },
        "arm_eligibility": _arm_eligibility(case_kind="CLEAN_CONTROL"),
    }


def _decision_capsule(
    base: Path,
    source: RegistrySource,
    item: Mapping[str, Any],
    cache: dict[tuple[str, str, str], dict[str, dict[str, Any]]],
    *,
    model_config_record_sha256: str,
) -> dict[str, Any]:
    _sha(model_config_record_sha256, "model_config_record_sha256")
    card = item["card"]
    reconstruction = item["reconstruction"]
    candidate = item["candidate"]
    focal_items = item.get("focal_items", [item])
    _require(
        isinstance(focal_items, Sequence) and focal_items,
        "focal_items_invalid",
    )
    target_step = candidate["exposure"]["target_step"]
    _require(
        all(
            focal_item["candidate"]["exposure"]["target_step"] == target_step
            for focal_item in focal_items
        ),
        "focal_target_decision_mismatch",
    )
    step = next(
        (record for record in reconstruction["steps"] if record["step_index"] == target_step),
        None,
    )
    _require(step is not None, "target_step_missing", task=card["task"]["task_name"])
    provenance = reconstruction["provenance"]
    run_root = _resolve_input(base, provenance["source_relative_run_path"])
    stream_relative = _relative_path(
        provenance["task_stream_relative_path"],
        "reconstruction.provenance.task_stream_relative_path",
    )
    stream_path = _safe_child(run_root, stream_relative)
    stream_bytes = _read_regular(stream_path)
    _require(
        _digest(stream_bytes) == provenance["task_stream_sha256"],
        "task_stream_drift",
        task=card["task"]["task_name"],
    )
    decision_event_id = step["P_t"]["decision_event_id"]
    cache_key = (str(run_root), stream_relative, decision_event_id)
    if cache_key not in cache:
        events = _load_jsonl_prefix_through_event(
            stream_bytes,
            stream_path,
            stop_event_id=decision_event_id,
        )
        cache[cache_key] = {event["event_id"]: event for event in events}
        _require(len(cache[cache_key]) == len(events), "raw_event_id_duplicate")
    events_by_id = cache[cache_key]
    request_event = _event(events_by_id, step["I_t"]["event_id"], "model_request")
    pre_event = _event(events_by_id, step["S_t"]["event_id"], "step_started")
    decision_event = _event(events_by_id, decision_event_id, "agent_decision")
    task_started_event = _event(
        events_by_id,
        reconstruction["task_started_event_id"],
        "task_started",
    )
    request_payload = request_event["payload"]
    request_view = request_payload.get("request_view")
    _require(
        isinstance(request_view, Mapping)
        and request_view.get("model") == item["served_model_name"],
        "request_served_model_mismatch",
    )
    request_view_sha = _digest(canonical_json_bytes(request_view, newline=False))
    _require(
        request_view_sha == step["I_t"]["request_view_sha256"],
        "request_view_drift",
        task=card["task"]["task_name"],
        step=target_step,
    )
    sdk_ref = request_payload.get("sdk_arguments_snapshot_blob")
    _require(sdk_ref == step["I_t"]["sdk_arguments_snapshot_blob"], "sdk_reference_drift")
    sdk_bytes = _read_verified_blob(run_root, sdk_ref)
    graph = _load_json_bytes(sdk_bytes, _blob_path(run_root, sdk_ref))
    _require(isinstance(graph, dict), "sdk_graph_invalid")
    rehydrated = ArtifactSerializer(BlobStore(run_root)).rehydrate(graph)
    _require(isinstance(rehydrated, Mapping), "sdk_arguments_not_mapping")
    sdk_canonical_sha = _digest(sdk_json(rehydrated))

    request_images = request_payload.get("request_images")
    _require(isinstance(request_images, list) and request_images, "request_images_missing")
    image_refs: list[dict[str, Any]] = []
    for index, image in enumerate(request_images):
        _require(isinstance(image, Mapping), "request_image_invalid", index=index)
        content_ref = image.get("content_blob")
        _read_verified_blob(run_root, content_ref)
        original_ref = image.get("original_text_blob")
        if original_ref is not None:
            _read_verified_blob(run_root, original_ref)
        image_refs.append(
            {
                "content_path": image.get("content_path"),
                "content_blob": _json_clone(content_ref),
                "original_text_blob": _json_clone(original_ref),
                "width": image.get("width"),
                "height": image.get("height"),
                "media_type": image.get("media_type"),
            }
        )

    pre_observation = pre_event["payload"].get("observation")
    _require(pre_observation == step["S_t"]["observation"], "current_gui_reconstruction_drift")
    current_ref = _current_gui_ref(pre_observation)
    _read_verified_blob(run_root, current_ref)
    _require(
        current_ref["digest"] in {record["content_blob"]["digest"] for record in image_refs},
        "current_gui_not_in_request",
    )

    action = decision_event["payload"].get("parsed_action")
    _require(action == step["A_t"]["parsed_action"], "parsed_action_reconstruction_drift")
    _validate_original_action(action, step["P_t"])

    resolved_spans = []
    for focal_item in focal_items:
        focal_candidate = focal_item["candidate"]
        span = _resolve_target_span(source.history_family, focal_candidate, request_view, step)
        span["target_set_entry"] = _target_set_entry(
            focal_candidate,
            span,
            request_event_id=request_event["event_id"],
        )
        resolved_spans.append(span)
    resolved_spans.sort(
        key=lambda value: (
            value["target_set_entry"]["request_path"],
            value["target_set_entry"]["char_start"],
            value["target_set_entry"]["char_end"],
            value["target_set_entry"]["candidate_id"],
        )
    )
    _validate_ordered_target_set([span["target_set_entry"] for span in resolved_spans])
    task_start_payload = task_started_event["payload"]
    _require(
        task_start_payload.get("task_name") == card["task"]["task_name"]
        and task_start_payload.get("task_goal") == card["instruction"],
        "task_start_payload_drift",
    )
    task_parameter_projection = {
        key: task_start_payload.get(key)
        for key in (
            "suite_family",
            "task_name",
            "task_index",
            "whole_task_attempt_index",
            "task_goal",
            "task_goal_status",
        )
    }
    capsule_identity = {
        "model_config_manifest_sha256": source.model_manifest_sha256,
        "model_config_record_sha256": model_config_record_sha256,
        "task_stream_sha256": provenance["task_stream_sha256"],
        "request_event_id": request_event["event_id"],
        "request_view_sha256": request_view_sha,
        "sdk_arguments_snapshot_sha256": sdk_ref["digest"],
        "sdk_arguments_canonical_sha256": sdk_canonical_sha,
        "current_gui_sha256": current_ref["digest"],
        "request_image_sha256s": [record["content_blob"]["digest"] for record in image_refs],
        "target_span_sha256s": [span["span_sha256"] for span in resolved_spans],
        "original_action_sha256": canonical_sha256(action),
        "task_parameters_sha256": canonical_sha256(task_parameter_projection),
        "request_cutoff_event_id": request_event["event_id"],
    }
    return {
        "task": {
            "catalog_index": card["task"]["catalog_index"],
            "task_name": card["task"]["task_name"],
            "task_run_id": card["task"]["task_run_id"],
            "task_instruction_sha256": _digest(card["instruction"].encode("utf-8")),
            "task_parameters_sha256": canonical_sha256(task_parameter_projection),
        },
        "decision": {
            "target_step": target_step,
            "step_id": step["step_id"],
            "request_event_id": request_event["event_id"],
            "request_id": request_payload.get("request_id"),
            "model_call_id": request_payload.get("model_call_id"),
            "decision_event_id": decision_event["event_id"],
            "request_cutoff": {
                "event_id": request_event["event_id"],
                "event_seq": request_event["seq"],
                "monotonic_ns": request_event["monotonic_ns"],
                "wall_time": request_event["wall_time"],
                "target_step": target_step,
            },
        },
        "source_locator": {
            "source_id": provenance["source_id"],
            "source_run_id": provenance["source_run_id"],
            "source_relative_run_path": provenance["source_relative_run_path"],
            "task_stream_relative_path": stream_relative,
            "task_stream_sha256": provenance["task_stream_sha256"],
        },
        "model_config": {
            "manifest_sha256": source.model_manifest_sha256,
            "record_sha256": model_config_record_sha256,
        },
        "request_view_sha256": request_view_sha,
        "sdk_arguments_snapshot_blob": _json_clone(sdk_ref),
        "sdk_arguments_canonical_sha256": sdk_canonical_sha,
        "current_gui_blob": _json_clone(current_ref),
        "request_images": image_refs,
        "backend_checkpoint": {
            "required": False,
            "reference": None,
            "reason": "model_call_is_fully_bound_to_captured_sdk_arguments_and_request_images",
        },
        "original_action": {
            "parsed_action": _json_clone(action),
            "parsed_action_sha256": canonical_sha256(action),
        },
        "resolved_target_spans": resolved_spans,
        "capsule_sha256": canonical_sha256(capsule_identity),
    }


def _resolve_target_span(
    history_family: str,
    candidate: Mapping[str, Any],
    request_view: Any,
    step: Mapping[str, Any],
) -> dict[str, Any]:
    exposure = candidate["exposure"]
    request_path = exposure["request_path"]
    expected_sha = exposure["span_sha256"]
    _require(isinstance(request_view, Mapping), "request_view_not_mapping")
    messages = request_view.get("messages")
    _require(isinstance(messages, list), "request_messages_missing")
    if history_family == "FLAT_PROGRESS":
        match = _QWEN_PATH_RE.fullmatch(request_path)
        _require(match is not None, "qwen_request_path_invalid", request_path=request_path)
        message_index = int(match.group("message"))
        block_index = int(match.group("block"))
        start = int(match.group("start"))
        end = int(match.group("end"))
        text = messages[message_index]["content"][block_index]["text"]
        _require(
            isinstance(text, str) and 0 <= start < end <= len(text), "qwen_span_bounds_invalid"
        )
        exact = text[start:end]
        _require(_digest(exact.encode("utf-8")) == expected_sha, "target_span_hash_mismatch")
        mapped = [
            record
            for record in step["I_t"]["assistant_exposures"]
            if record.get("mapping_status") == _ALLOWED_MAPPING[history_family]
            and record.get("span_start") == start
            and record.get("span_end") == end
            and record.get("exposed_text_sha256") == expected_sha
        ]
        _require(len(mapped) == 1, "qwen_exposure_mapping_not_unique")
        record = mapped[0]
        step_start = record.get("step_span_start")
        step_end = record.get("step_span_end")
        _require(
            isinstance(step_start, int)
            and isinstance(step_end, int)
            and 0 <= step_start <= start < end <= step_end <= len(text),
            "qwen_enclosing_step_bounds_invalid",
        )
        enclosing = text[step_start:step_end]
        _require(
            _digest(enclosing.encode("utf-8")) == record.get("step_span_sha256"),
            "qwen_enclosing_step_hash_mismatch",
        )
        return {
            "request_path": request_path,
            "record_path": (
                f"payload.request_view.messages[{message_index}].content[{block_index}].text"
            ),
            "mapping_status": _ALLOWED_MAPPING[history_family],
            "message_index": message_index,
            "content_block_index": block_index,
            "char_start": start,
            "char_end": end,
            "utf8_byte_start": len(text[:start].encode("utf-8")),
            "utf8_byte_end": len(text[:end].encode("utf-8")),
            "span_sha256": expected_sha,
            "container_sha256": _digest(text.encode("utf-8")),
            "claim_text": exact,
            "conclusion_text": exact,
            "enclosing_step_span": {
                "span_start": step_start,
                "span_end": step_end,
                "span_sha256": record["step_span_sha256"],
                "text": enclosing,
            },
        }

    _require(history_family == "RAW_REPLAY", "history_family_invalid")
    match = _RAW_PATH_RE.fullmatch(request_path)
    _require(match is not None, "raw_request_path_invalid", request_path=request_path)
    message_index = int(match.group("message"))
    exact = messages[message_index]["content"]
    _require(isinstance(exact, str), "raw_request_span_not_text")
    # The audit card binds the complete raw assistant record.  G1.1 must not
    # promote a normalized claim or a syntactically convenient ``thinking``
    # element into an executable treatment span.  Exact edit spans are an
    # independently double-reviewed G1.6 artifact.
    _require(_digest(exact.encode("utf-8")) == expected_sha, "target_span_hash_mismatch")
    source_step = candidate["claim"]["source_steps"][-1]
    mapped = [
        record
        for record in step["I_t"]["assistant_exposures"]
        if record.get("mapping_status") == _ALLOWED_MAPPING[history_family]
        and record.get("message_index") == message_index
        and record.get("source_step_index") == source_step
    ]
    _require(len(mapped) == 1, "raw_exposure_mapping_not_unique")
    envelope: dict[str, Any] | None = None
    if exact.count("<thinking>") == 1 and exact.count("</thinking>") == 1:
        envelope_start = exact.index("<thinking>") + len("<thinking>")
        envelope_end = exact.index("</thinking>", envelope_start)
        while envelope_start < envelope_end and exact[envelope_start].isspace():
            envelope_start += 1
        while envelope_end > envelope_start and exact[envelope_end - 1].isspace():
            envelope_end -= 1
        if envelope_start < envelope_end:
            envelope_text = exact[envelope_start:envelope_end]
            envelope = {
                "char_start": envelope_start,
                "char_end": envelope_end,
                "utf8_byte_start": len(exact[:envelope_start].encode("utf-8")),
                "utf8_byte_end": len(exact[:envelope_end].encode("utf-8")),
                "span_sha256": _digest(envelope_text.encode("utf-8")),
                "editable": False,
                "purpose": "NON_EDITABLE_G1_6_CURATION_ENVELOPE",
            }
    return {
        "request_path": request_path,
        "record_path": request_path,
        "mapping_status": _ALLOWED_MAPPING[history_family],
        "message_index": message_index,
        "content_block_index": None,
        "char_start": 0,
        "char_end": len(exact),
        "utf8_byte_start": 0,
        "utf8_byte_end": len(exact.encode("utf-8")),
        "span_sha256": expected_sha,
        "container_sha256": expected_sha,
        "raw_request_text": exact,
        "edit_span_status": "G1_6_PENDING",
        "focal_edit_spans": [],
        "curation_envelope": envelope,
        "enclosing_step_span": None,
    }


def _target_set_entry(
    candidate: Mapping[str, Any],
    resolved_span: Mapping[str, Any],
    *,
    request_event_id: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "request_path": resolved_span["record_path"],
        "record_identity_sha256": _stable_hash(
            "request-record",
            request_event_id,
            resolved_span["record_path"],
            resolved_span["container_sha256"],
        ),
        "container_sha256": resolved_span["container_sha256"],
        "char_start": resolved_span["char_start"],
        "char_end": resolved_span["char_end"],
        "utf8_byte_start": resolved_span["utf8_byte_start"],
        "utf8_byte_end": resolved_span["utf8_byte_end"],
        "span_sha256": resolved_span["span_sha256"],
        "edit_span_status": resolved_span.get("edit_span_status", "G1_1_FROZEN"),
        "focal_edit_spans": _json_clone(
            resolved_span.get(
                "focal_edit_spans",
                [
                    {
                        "char_start": resolved_span["char_start"],
                        "char_end": resolved_span["char_end"],
                        "utf8_byte_start": resolved_span["utf8_byte_start"],
                        "utf8_byte_end": resolved_span["utf8_byte_end"],
                        "span_sha256": resolved_span["span_sha256"],
                    }
                ],
            )
        ),
        "curation_envelope": _json_clone(resolved_span.get("curation_envelope")),
    }


def _validate_ordered_target_set(targets: Sequence[Mapping[str, Any]]) -> None:
    identities = [
        (
            target.get("request_path"),
            target.get("char_start"),
            target.get("char_end"),
            target.get("candidate_id"),
        )
        for target in targets
    ]
    _require(identities == sorted(identities), "focal_target_set_not_ordered")
    _require(len(identities) == len(set(identities)), "focal_target_set_duplicate")
    by_record: dict[tuple[Any, Any], list[tuple[int, int]]] = defaultdict(list)
    for target in targets:
        start = target.get("char_start")
        end = target.get("char_end")
        _require(
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end,
            "focal_target_offsets_invalid",
        )
        edit_spans = target.get("focal_edit_spans")
        _require(isinstance(edit_spans, list), "focal_edit_spans_invalid")
        status = target.get("edit_span_status")
        _require(status in {"G1_1_FROZEN", "G1_6_PENDING"}, "edit_span_status_invalid")
        _require(
            (status == "G1_1_FROZEN" and len(edit_spans) == 1)
            or (status == "G1_6_PENDING" and not edit_spans),
            "edit_span_status_cardinality_mismatch",
        )
        for edit_span in edit_spans:
            edit_start = edit_span.get("char_start")
            edit_end = edit_span.get("char_end")
            _require(
                isinstance(edit_start, int)
                and not isinstance(edit_start, bool)
                and isinstance(edit_end, int)
                and not isinstance(edit_end, bool)
                and start <= edit_start < edit_end <= end,
                "focal_edit_span_offsets_invalid",
            )
            by_record[(target.get("request_path"), target.get("record_identity_sha256"))].append(
                (edit_start, edit_end)
            )
    for intervals in by_record.values():
        ordered = sorted(intervals)
        _require(
            all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
            "focal_target_spans_overlap",
        )


def _target_history(
    source: RegistrySource,
    item: Mapping[str, Any],
    resolved_span: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = item["candidate"]
    return {
        "candidate_id": candidate["candidate_id"],
        "representation_type": candidate["claim"]["representation_type"],
        "provenance_confidence": candidate["claim"]["provenance_confidence"],
        "source_steps": candidate["claim"]["source_steps"],
        "request_path": resolved_span["record_path"],
        "source_record_sha256": resolved_span["container_sha256"],
        "char_start": resolved_span["char_start"],
        "char_end": resolved_span["char_end"],
        "utf8_byte_start": resolved_span["utf8_byte_start"],
        "utf8_byte_end": resolved_span["utf8_byte_end"],
        "span_sha256": resolved_span["span_sha256"],
        "edit_span_status": resolved_span.get("edit_span_status", "G1_1_FROZEN"),
        "focal_edit_spans": _json_clone(
            resolved_span.get(
                "focal_edit_spans",
                [
                    {
                        "char_start": resolved_span["char_start"],
                        "char_end": resolved_span["char_end"],
                        "utf8_byte_start": resolved_span["utf8_byte_start"],
                        "utf8_byte_end": resolved_span["utf8_byte_end"],
                        "span_sha256": resolved_span["span_sha256"],
                    }
                ],
            )
        ),
        "curation_envelope": _json_clone(resolved_span.get("curation_envelope")),
        "transform_binding": (
            "raw_record_hash_with_g1_6_pending_edit_span"
            if source.history_family == "RAW_REPLAY"
            else "conclusion_span_offsets_plus_enclosing_step_span"
        ),
    }


def _gate_facts(
    card: Mapping[str, Any],
    review: Mapping[str, Any],
    candidate: Mapping[str, Any],
    chain: Mapping[str, Any],
) -> dict[str, dict[str, bool]]:
    coverage = card["coverage"]
    complete = (
        review["coverage_verdict"] == "SUFFICIENT"
        and coverage["integrity_valid"] is True
        and coverage["capture_complete"] is True
        and coverage["reconstructed_decision_count"] == coverage["decision_count"]
        and coverage["dropped_candidate_count"] == 0
    )
    common = {
        "coverage_complete": complete,
        "actual_exposure": candidate["exposure"]["was_actually_in_request"] is True,
        "provenance_exact_or_high": candidate["claim"]["provenance_confidence"]
        in _STRICT_PROVENANCE,
        "explicit_use": chain["uptake_evidence"] == "EXPLICIT_USE",
    }
    return {
        "strict_mhr": {
            **common,
            "validity_refuted_or_stale": chain["history_validity"] in _STRICT_VALIDITY,
            "low_state_confound": chain["state_confound"] in _LOW_CONFOUND,
        },
        "clean_control": {
            **common,
            "validity_supported": chain["history_validity"] == "SUPPORTED",
            "no_visible_harm": chain["downstream_effects"] == ["NO_VISIBLE_HARM"],
        },
    }


def _select_clean_controls(
    pool: Sequence[Mapping[str, Any]], *, target: int, minimum_tasks: int
) -> list[Mapping[str, Any]]:
    _require(target <= len(pool), "clean_control_pool_too_small", target=target, pool=len(pool))
    by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in pool:
        by_task[item["task_name"]].append(item)
    _require(minimum_tasks <= len(by_task), "clean_control_task_pool_too_small")
    task_representatives = sorted(
        (min(items, key=lambda item: item["rank"]) for items in by_task.values()),
        key=lambda item: item["rank"],
    )
    selected = list(task_representatives[:minimum_tasks])
    selected_ids = {(item["task_name"], item["candidate"]["candidate_id"]) for item in selected}
    for item in sorted(pool, key=lambda value: value["rank"]):
        if len(selected) >= target:
            break
        item_key = (item["task_name"], item["candidate"]["candidate_id"])
        if item_key not in selected_ids:
            selected.append(item)
            selected_ids.add(item_key)
    return selected


def _ledger_record(
    source: RegistrySource,
    item: Mapping[str, Any],
    disposition: str,
    reasons: Sequence[str],
    *,
    model_config_record_sha256: str,
    unit_kind: str | None,
    unit_id: str | None,
) -> dict[str, Any]:
    candidate = item["candidate"]
    card = item["card"]
    identity = _stable_hash(
        "selection-ledger",
        PROTOCOL_VERSION,
        source.source_key,
        source.model_manifest_sha256,
        model_config_record_sha256,
        card["task"]["task_run_id"],
        candidate["candidate_id"],
        unit_id or "EXCLUDED",
    )
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "record_type": "causal_replay_selection_ledger",
        "protocol_version": PROTOCOL_VERSION,
        "ledger_id": f"g1ledger-{identity[:24]}",
        "curated": True,
        "deployment_prediction": False,
        "source_key": source.source_key,
        "model_id": source.model_id,
        "model_config_manifest_sha256": source.model_manifest_sha256,
        "model_config_record_sha256": model_config_record_sha256,
        "task_name": item["task_name"],
        "task_run_id": card["task"]["task_run_id"],
        "target_step": candidate["exposure"]["target_step"],
        "candidate_id": candidate["candidate_id"],
        "candidate_sha256": canonical_sha256(candidate),
        "unit_kind": unit_kind,
        "unit_id": unit_id,
        "disposition": disposition,
        "reason_codes": sorted(reasons),
        "gate_facts": item["gates"],
    }


def _exclusion_reasons(gates: Mapping[str, Mapping[str, bool]]) -> list[str]:
    strict = gates["strict_mhr"]
    reasons: set[str] = set()
    if not strict["coverage_complete"] or not strict["actual_exposure"]:
        reasons.add("SOURCE_REFERENCE_UNRESOLVED")
    if not strict["provenance_exact_or_high"]:
        reasons.add("PROVENANCE_BELOW_HIGH")
    if not strict["validity_refuted_or_stale"]:
        reasons.add("NOT_REFUTED_OR_STALE")
    if not strict["explicit_use"]:
        reasons.add("NO_EXPLICIT_UPTAKE")
    if not strict["low_state_confound"]:
        reasons.add("NOT_STRICT_MHR")
    _require(bool(reasons), "excluded_candidate_has_no_frozen_reason")
    _require(reasons <= LEDGER_REASON_CODES, "ledger_reason_code_not_frozen")
    return sorted(reasons)


def _validate_reviews(
    cards: Mapping[str, Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    by_task: dict[str, Mapping[str, Any]] = {}
    review_ids: set[str] = set()
    for review in reviews:
        _require(isinstance(review, Mapping), "review_invalid")
        task_name = review.get("task_name")
        _require(isinstance(task_name, str) and task_name in cards, "review_task_unknown")
        _require(task_name not in by_task, "review_task_duplicate", task=task_name)
        _require(review.get("review_id") not in review_ids, "review_id_duplicate")
        review_ids.add(review.get("review_id"))
        card = cards[task_name]
        _require(
            review.get("catalog_index") == card["task"]["catalog_index"], "review_catalog_drift"
        )
        _require(review.get("card_sha256") == canonical_sha256(card), "review_card_hash_drift")
        chains = review.get("chains")
        _require(isinstance(chains, list), "review_chains_invalid")
        candidate_ids = [candidate["candidate_id"] for candidate in card["candidates"]]
        _require(
            [chain.get("candidate_id") for chain in chains] == candidate_ids,
            "review_candidate_coverage_drift",
            task=task_name,
        )
        by_task[task_name] = review
    _require(set(by_task) == set(cards), "review_task_coverage_drift")
    return by_task


def _validate_reconstructions(
    cards: Mapping[str, Mapping[str, Any]], recons: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    by_task: dict[str, Mapping[str, Any]] = {}
    for reconstruction in recons:
        task_name = reconstruction.get("task_name")
        _require(isinstance(task_name, str) and task_name in cards, "reconstruction_task_unknown")
        _require(task_name not in by_task, "reconstruction_task_duplicate")
        card = cards[task_name]
        _require(
            canonical_sha256(reconstruction) == card["coverage"]["full_reconstruction_sha256"],
            "reconstruction_hash_drift",
            task=task_name,
        )
        forbidden_keys = {"outcome", "environment_evaluation", "failure_link"}
        _require(not forbidden_keys.intersection(reconstruction), "future_evidence_leakage")
        by_task[task_name] = reconstruction
    _require(set(by_task) == set(cards), "reconstruction_task_coverage_drift")
    return by_task


def _verify_raw_manifests(base: Path, curated: Mapping[str, Any]) -> None:
    sources = curated.get("sources")
    _require(isinstance(sources, list) and sources, "curated_sources_invalid")
    for source in sources:
        _require(isinstance(source, Mapping), "curated_source_invalid")
        run_root = _resolve_input(base, source.get("relative_run_path"))
        for field in ("manifest_start", "manifest_final", "run_events"):
            summary = source.get(field)
            _require(isinstance(summary, Mapping), "raw_manifest_summary_invalid", field=field)
            relative = _relative_path(summary.get("relative_path"), f"curated.sources.{field}")
            data = _read_regular(_safe_child(run_root, relative))
            _verify_summary(data, summary, path=f"{source.get('source_id')}:{relative}")


def _arm_catalog() -> dict[str, Any]:
    arms = [
        {
            "arm_id": "ORIGINAL",
            "case_kinds": ["STRICT_MHR", "CLEAN_CONTROL"],
            "edit": "none; submit the hash-pinned SDK request exactly",
            "operation": "NONE",
            "allowed_deltas": ["REPLAY_SEED", "TRANSPORT_VOLATILES"],
            "invariants": [
                "request content unchanged except the one pre-registered replay seed and transport volatiles",
                "the replay seed is fixed across every arm in a case-repeat block",
                "images and message order unchanged",
            ],
        },
        {
            "arm_id": "MASK",
            "case_kinds": ["STRICT_MHR"],
            "edit": "remove the complete ordered focal target set and no other span",
            "operation": "DELETE",
            "allowed_deltas": ["REGISTERED_HISTORY_EDIT", "REPLAY_SEED", "TRANSPORT_VOLATILES"],
            "invariants": [
                "delete every complete registered span in the ordered focal target set",
                "apply only the pre-registered delimiter repair",
                "all non-target bytes, message blocks, images, and ordering unchanged",
            ],
        },
        {
            "arm_id": "MASK_CORRECTION",
            "case_kinds": ["STRICT_MHR"],
            "edit": "MASK, then insert the independently curated correction at the same location",
            "operation": "DELETE_THEN_INSERT",
            "allowed_deltas": ["REGISTERED_HISTORY_EDIT", "REPLAY_SEED", "TRANSPORT_VOLATILES"],
            "invariants": [
                "correction hash must resolve",
                "no target response or post-state evidence",
            ],
        },
        {
            "arm_id": "ORACLE_CLEAN",
            "case_kinds": ["STRICT_MHR"],
            "edit": "remove every independently registered relevant misleading premise; retain all other history",
            "operation": "DELETE_ALL_RELEVANT_MISLEADING_PREMISES",
            "allowed_deltas": ["REGISTERED_HISTORY_EDIT", "REPLAY_SEED", "TRANSPORT_VOLATILES"],
            "invariants": [
                "every removed premise is hash-registered before replay",
                "non-misleading history, current GUI, and task request remain unchanged",
            ],
        },
        {
            "arm_id": "SHAM_BENIGN_EDIT",
            "case_kinds": ["STRICT_MHR", "CLEAN_CONTROL"],
            "edit": "delete one independently registered benign span matched to MASK",
            "operation": "DELETE",
            "allowed_deltas": ["REGISTERED_HISTORY_EDIT", "REPLAY_SEED", "TRANSPORT_VOLATILES"],
            "invariants": [
                "delete the complete registered benign span and apply the exact same delimiter-repair rule as MASK",
                "benign edit hash must resolve",
                "pinned tokenizer token count must match MASK",
                "exact structural-location bucket must match MASK",
                "gold meaning preserved",
            ],
        },
    ]
    for arm in arms:
        arm["curated"] = True
        arm["deployment_prediction"] = False
    return {
        "schema_version": ARM_SCHEMA_VERSION,
        "record_type": "causal_replay_arm_catalog",
        "protocol_version": PROTOCOL_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "schedule": {
            "block_count": 6,
            "hash_salt": ARM_ORDER_SALT,
            "hash_input_bytes": "UTF-8 literal salt|model_id|unit_id",
            "base_rotation_rule": "sha256(input).digest[0] % arm_count",
            "direction_rule": "+1 if digest[1] % 2 == 0 else -1",
            "order_rule": "for zero-based block b and within-block position j: base_arms[(j + initial_rotation + direction*b) % arm_count]",
            "reverse_on_alternate_repeat": False,
            "seed_rule": "one pre-registered replay seed fixed for all arms in the same case-repeat block",
            "unit_kind_schedules": {
                "STRICT_MHR": {
                    "unit_id_field": "case_id",
                    "base_arms": list(ARM_IDS),
                    "arm_count": 5,
                    "base_rotation_modulus": 5,
                },
                "CLEAN_CONTROL": {
                    "unit_id_field": "control_id",
                    "base_arms": ["ORIGINAL", "SHAM_BENIGN_EDIT"],
                    "arm_count": 2,
                    "base_rotation_modulus": 2,
                },
            },
        },
        "sham_matching": {
            "tokenizer_ref_required": True,
            "token_count_required": True,
            "location_bucket_required": True,
            "operation": "DELETE",
        },
        "arms": arms,
    }


def _arm_eligibility(*, case_kind: str) -> list[dict[str, Any]]:
    result = []
    for arm in _arm_catalog()["arms"]:
        applicable = case_kind in arm["case_kinds"]
        result.append(
            {
                "arm_id": arm["arm_id"],
                "structurally_applicable": applicable,
                "execution_ready": False,
                "reason": (
                    "G1_6_GOLD_AND_TRANSFORMATION_PENDING"
                    if applicable
                    else "CASE_KIND_NOT_APPLICABLE"
                ),
            }
        )
    return result


def arm_order(*, model_id: str, case_id: str, block_index: int) -> tuple[str, ...]:
    """Return the preregistered arm order for one of six case-repeat blocks."""

    _require(model_id in _EXPECTED_CLEAN_SELECTION, "arm_order_model_invalid")
    _require(
        isinstance(case_id, str) and re.fullmatch(r"g1case-[0-9a-f]{24}", case_id) is not None,
        "arm_order_case_id_invalid",
    )
    _require(
        isinstance(block_index, int)
        and not isinstance(block_index, bool)
        and 1 <= block_index <= 6,
        "arm_order_block_invalid",
    )
    return _arm_order_for_unit(
        model_id=model_id,
        unit_id=case_id,
        unit_kind="STRICT_MHR",
        block_index=block_index,
    )


def _arm_order_for_unit(
    *, model_id: str, unit_id: str, unit_kind: str, block_index: int
) -> tuple[str, ...]:
    _require(model_id in _EXPECTED_CLEAN_SELECTION, "arm_order_model_invalid")
    expected_pattern = (
        r"g1case-[0-9a-f]{24}" if unit_kind == "STRICT_MHR" else r"g1control-[0-9a-f]{24}"
    )
    _require(
        unit_kind in {"STRICT_MHR", "CLEAN_CONTROL"}
        and isinstance(unit_id, str)
        and re.fullmatch(expected_pattern, unit_id) is not None,
        "arm_order_unit_invalid",
    )
    _require(
        isinstance(block_index, int)
        and not isinstance(block_index, bool)
        and 1 <= block_index <= 6,
        "arm_order_block_invalid",
    )
    unit_schedule = _arm_catalog()["schedule"]["unit_kind_schedules"][unit_kind]
    arms = tuple(unit_schedule["base_arms"])
    _require(
        len(arms) == unit_schedule["arm_count"] == unit_schedule["base_rotation_modulus"],
        "arm_catalog_unit_schedule_invalid",
    )
    digest = hashlib.sha256(f"{ARM_ORDER_SALT}|{model_id}|{unit_id}".encode()).digest()
    base = digest[0] % len(arms)
    direction = 1 if digest[1] % 2 == 0 else -1
    start = (base + direction * (block_index - 1)) % len(arms)
    return tuple(arms[(start + offset) % len(arms)] for offset in range(len(arms)))


def validate_run_record(record: Mapping[str, Any]) -> None:
    """Validate a G1.7 immutable run plan's closed schedule and isolation contract."""

    _require(record.get("status") == "PLANNED", "run_plan_status_invalid")
    _validate_static_schema("run.schema.json", record)
    expected_order = _arm_order_for_unit(
        model_id=record["model_id"],
        unit_id=record["case_id"],
        unit_kind=record["unit_kind"],
        block_index=record["block_index"],
    )
    exact_input = f"{ARM_ORDER_SALT}|{record['model_id']}|{record['case_id']}".encode()
    _require(
        record["arm_order_input_sha256"] == _digest(exact_input),
        "run_arm_order_input_hash_mismatch",
    )
    _require(
        record["block_arm_order"] == list(expected_order)
        and record["block_arm_order_sha256"] == canonical_sha256(list(expected_order)),
        "run_block_arm_order_mismatch",
    )
    _require(
        record["block_arm_order"][record["arm_order_index"]] == record["arm_id"],
        "run_arm_order_index_mismatch",
    )
    block_contract = {
        1: (1729, 1),
        2: (1729, 2),
        3: (2718, 1),
        4: (2718, 2),
        5: (31415, 1),
        6: (31415, 2),
    }
    _require(
        (record["replay_seed"], record["repeat_index"]) == block_contract[record["block_index"]],
        "run_block_seed_repeat_mismatch",
    )
    _require(
        record["model_config_manifest_sha256"] == MODEL_CONFIG_MANIFEST_SHA256,
        "run_model_config_manifest_mismatch",
    )


def _attempt_record_hash(attempt: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {key: value for key, value in attempt.items() if key != "attempt_record_sha256"}
    )


def validate_outcome_record(record: Mapping[str, Any], *, run_record: Mapping[str, Any]) -> None:
    """Validate outcome precedence and its exact binding to one run record."""

    _validate_static_schema("outcome.schema.json", record)
    validate_run_record(run_record)
    _require(
        record["run_record_sha256"] == canonical_sha256(run_record),
        "outcome_run_record_hash_mismatch",
    )
    direct_fields = ("run_id", "unit_kind", "case_id", "model_id", "arm_id")
    for field in direct_fields:
        _require(record[field] == run_record[field], "outcome_run_identity_mismatch", field=field)
    hash_fields = (
        "unit_record_sha256",
        "admission_record_sha256",
        "run_ready_seal_sha256",
        "frozen_capsule_sha256",
        "arm_plan_sha256",
        "action_gold_bundle_sha256",
        "model_config_manifest_sha256",
        "parser_manifest_sha256",
        "scorer_manifest_sha256",
        "schedule_manifest_sha256",
        "request_sha256",
    )
    for field in hash_fields:
        _require(
            record[field] == run_record[field], "outcome_run_hash_binding_mismatch", field=field
        )
    attempts = record["attempts"]
    _require(
        record["attempts_sha256"] == canonical_sha256(attempts), "outcome_attempts_hash_mismatch"
    )
    for attempt in attempts:
        _require(
            attempt["request_sha256"] == run_record["request_sha256"]
            and attempt["request_byte_count"] == run_record["request_byte_count"],
            "outcome_attempt_request_mismatch",
        )
        _require(
            attempt["attempt_record_sha256"] == _attempt_record_hash(attempt),
            "outcome_attempt_record_hash_mismatch",
        )
    final_attempt = attempts[-1]
    if record["status"] == "COMPLETED":
        _require(
            final_attempt["status"] == "COMPLETED"
            and final_attempt["response_blob"] == record["response_blob"],
            "outcome_final_response_mismatch",
        )
        if record["parse_class"] == "PARSEABLE_ACTION":
            action_blob = record["action_blob"]
            parser_result = record["parser_result"]
            _require(
                isinstance(action_blob, Mapping)
                and action_blob.get("schema_version") == "mobileworld.g1.normalized-action/v1"
                and isinstance(parser_result, Mapping),
                "outcome_parseable_action_binding_invalid",
            )
            _require(
                action_blob.get("sha256") == parser_result.get("normalized_action_sha256"),
                "outcome_normalized_action_hash_mismatch",
            )
    else:
        _require(
            final_attempt["status"] == "FAILED"
            and record["response_blob"] is None
            and record["action_blob"] is None,
            "missing_outcome_payload_present",
        )


def _forbidden_projection_paths(value: Any, *, path: str = "$") -> tuple[str, ...]:
    """Locate post-target/future fields embedded in a registry projection."""

    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_PROJECTED_FUTURE_KEYS or normalized.startswith(
                (
                    "downstream_effect_",
                    "evaluator_",
                    "outcome_",
                    "target_post_",
                    "treatment_response_",
                )
            ):
                found.append(child_path)
            found.extend(_forbidden_projection_paths(child, path=child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found.extend(_forbidden_projection_paths(child, path=f"{path}[{index}]"))
    return tuple(found)


def _pending_evidence_channels_valid(record: Mapping[str, Any]) -> bool:
    gold = record.get("action_gold_refs")
    transformations = record.get("transformation_refs")
    if not isinstance(gold, Mapping) or not isinstance(transformations, Mapping):
        return False
    if set(gold) != {
        "accepted_next_action_set_ref",
        "review_ledger_ref",
        "curator_identity_ref",
        "curator_view",
        "allowed_evidence_roles",
        "forbidden_evidence_roles",
        "curation_phase",
    }:
        return False
    if not (
        gold.get("accepted_next_action_set_ref") is None
        and gold.get("review_ledger_ref") is None
        and gold.get("curator_identity_ref") is None
        and gold.get("curator_view") == "TASK_AND_PRE_CALL_GUI_ONLY"
        and gold.get("allowed_evidence_roles")
        == ["ask_user_response", "target_pre", "task_instruction", "tool_response"]
        and gold.get("forbidden_evidence_roles") == sorted(_ACTION_GOLD_FORBIDDEN)
        and gold.get("curation_phase") == "G1_6_PENDING"
    ):
        return False

    expected_transformation_keys = {
        "transformation_plan_ref",
        "review_ledger_ref",
        "sham_benign_edit_ref",
        "curator_identity_ref",
        "curator_view",
        "audited_exposure_bindings",
        "focal_target_set",
        "focal_target_set_status",
        "oracle_target_set_ref",
        "forbidden_evidence_roles",
        "curation_phase",
    }
    if record.get("record_type") == "causal_replay_case":
        expected_transformation_keys.update({"mask_correction_ref", "oracle_clean_ref"})
    if set(transformations) != expected_transformation_keys:
        return False
    nullable_refs = expected_transformation_keys.intersection(
        {
            "mask_correction_ref",
            "oracle_clean_ref",
            "sham_benign_edit_ref",
            "transformation_plan_ref",
            "review_ledger_ref",
            "curator_identity_ref",
            "oracle_target_set_ref",
        }
    )
    bindings = transformations.get("audited_exposure_bindings")
    return bool(
        all(transformations.get(field) is None for field in nullable_refs)
        and transformations.get("curator_view")
        == "SOURCE_HISTORY_AND_PRE_CALL_GUI_NO_TARGET_DECISION"
        and transformations.get("forbidden_evidence_roles") == sorted(_TRANSFORMATION_FORBIDDEN)
        and transformations.get("curation_phase") == "G1_6_PENDING"
        and transformations.get("focal_target_set") == []
        and transformations.get("focal_target_set_status") == "G1_6_PENDING"
        and isinstance(bindings, list)
        and bool(bindings)
        and all(_pre_gold_binding_valid(target) for target in bindings)
    )


def _pre_gold_binding_valid(target: Any) -> bool:
    if not isinstance(target, Mapping) or set(target) != {
        "candidate_id",
        "request_path",
        "record_identity_sha256",
        "container_sha256",
        "char_start",
        "char_end",
        "utf8_byte_start",
        "utf8_byte_end",
        "span_sha256",
        "edit_span_status",
        "focal_edit_spans",
        "curation_envelope",
    }:
        return False
    start = target.get("char_start")
    end = target.get("char_end")
    byte_start = target.get("utf8_byte_start")
    byte_end = target.get("utf8_byte_end")
    if not (
        isinstance(target.get("candidate_id"), str)
        and bool(target["candidate_id"])
        and isinstance(target.get("request_path"), str)
        and bool(target["request_path"])
        and all(
            isinstance(target.get(field), str) and _SHA_RE.fullmatch(target[field]) is not None
            for field in ("record_identity_sha256", "container_sha256", "span_sha256")
        )
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
        and isinstance(byte_start, int)
        and not isinstance(byte_start, bool)
        and isinstance(byte_end, int)
        and not isinstance(byte_end, bool)
        and 0 <= byte_start < byte_end
        and isinstance(target.get("focal_edit_spans"), list)
    ):
        return False
    status = target.get("edit_span_status")
    edits = target["focal_edit_spans"]
    if status == "G1_6_PENDING":
        return not edits
    if status != "G1_1_FROZEN" or len(edits) != 1:
        return False
    edit = edits[0]
    return bool(
        isinstance(edit, Mapping)
        and set(edit)
        == {"char_start", "char_end", "utf8_byte_start", "utf8_byte_end", "span_sha256"}
        and isinstance(edit.get("char_start"), int)
        and isinstance(edit.get("char_end"), int)
        and start <= edit["char_start"] < edit["char_end"] <= end
        and isinstance(edit.get("utf8_byte_start"), int)
        and isinstance(edit.get("utf8_byte_end"), int)
        and byte_start <= edit["utf8_byte_start"] < edit["utf8_byte_end"] <= byte_end
        and isinstance(edit.get("span_sha256"), str)
        and _SHA_RE.fullmatch(edit["span_sha256"]) is not None
    )


def _validate_pre_gold_unit_identity(record: Mapping[str, Any]) -> None:
    record_type = record.get("record_type")
    if record_type == "causal_replay_case":
        identity_kind = "strict-mhr-case"
        identifier = record.get("case_id")
        identifier_prefix = "g1case-"
    else:
        _require(
            record_type == "causal_replay_clean_control",
            "pre_gold_unit_record_type_invalid",
        )
        identity_kind = "clean-control"
        identifier = record.get("control_id")
        identifier_prefix = "g1control-"
    manifest_sha256 = _sha(
        record.get("model_config_manifest_sha256"),
        "pre_gold.model_config_manifest_sha256",
    )
    _require(
        manifest_sha256 == MODEL_CONFIG_MANIFEST_SHA256,
        "pre_gold_model_config_manifest_mismatch",
    )
    model_record_sha256 = _sha(
        record.get("model_config_record_sha256"),
        "pre_gold.model_config_record_sha256",
    )
    model_id = _string(record.get("model_id"), "pre_gold.model_id")
    _require(
        _MODEL_CONFIG_RECORD_SHA256.get(model_id) == model_record_sha256,
        "pre_gold_model_config_record_mismatch",
    )
    _validate_pre_gold_model_assignment(record)
    _validate_pre_gold_arm_eligibility(record)
    task = record.get("task")
    decision = record.get("decision")
    capsule = record.get("frozen_capsule")
    _require(
        isinstance(task, Mapping)
        and isinstance(decision, Mapping)
        and isinstance(capsule, Mapping),
        "pre_gold_identity_fields_invalid",
    )
    _require(
        capsule.get("model_config")
        == {
            "manifest_sha256": manifest_sha256,
            "record_sha256": model_record_sha256,
        },
        "pre_gold_model_config_crossbind_mismatch",
    )
    expected = _paired_unit_hash(
        identity_kind,
        model_config_manifest_sha256=manifest_sha256,
        model_config_record_sha256=model_record_sha256,
        task_run_id=_string(task.get("task_run_id"), "pre_gold.task_run_id"),
        target_step=_positive_int(decision.get("target_step"), "pre_gold.target_step"),
        request_event_id=_string(decision.get("request_event_id"), "pre_gold.request_event_id"),
        request_view_sha256=_sha(
            capsule.get("request_view_sha256"), "pre_gold.request_view_sha256"
        ),
        current_gui_sha256=_sha(
            capsule.get("current_gui_blob", {}).get("digest"),
            "pre_gold.current_gui_sha256",
        ),
        sdk_arguments_snapshot_sha256=_sha(
            capsule.get("sdk_arguments_snapshot_blob", {}).get("digest"),
            "pre_gold.sdk_arguments_snapshot_sha256",
        ),
    )
    _require(
        identifier == f"{identifier_prefix}{expected[:24]}",
        "pre_gold_unit_id_mismatch",
    )


def _validate_pre_gold_model_assignment(record: Mapping[str, Any]) -> None:
    model_id = _string(record.get("model_id"), "pre_gold.model_id")
    expected = _PRE_GOLD_MODEL_ASSIGNMENTS.get(model_id)
    _require(expected is not None, "pre_gold_model_id_invalid")
    (
        expected_role,
        expected_history,
        expected_span_status,
        expected_representation,
        expected_transform_binding,
    ) = expected
    _require(
        record.get("study_role") == expected_role
        and record.get("history_family") == expected_history,
        "pre_gold_model_role_history_mismatch",
    )
    targets = record.get("target_histories")
    _require(
        isinstance(targets, list) and bool(targets),
        "pre_gold_target_histories_invalid",
    )
    for target in targets:
        _require(isinstance(target, Mapping), "pre_gold_target_history_invalid")
        edit_spans = target.get("focal_edit_spans")
        _require(isinstance(edit_spans, list), "pre_gold_focal_edit_spans_invalid")
        _require(
            target.get("edit_span_status") == expected_span_status
            and target.get("representation_type") == expected_representation
            and target.get("transform_binding") == expected_transform_binding
            and (
                (expected_span_status == "G1_1_FROZEN" and len(edit_spans) == 1)
                or (expected_span_status == "G1_6_PENDING" and not edit_spans)
            ),
            "pre_gold_history_span_contract_mismatch",
        )


def _validate_pre_gold_arm_eligibility(record: Mapping[str, Any]) -> None:
    record_type = record.get("record_type")
    _require(
        record_type in {"causal_replay_case", "causal_replay_clean_control"},
        "pre_gold_arm_eligibility_record_type_invalid",
    )
    case_kind = "STRICT_MHR" if record_type == "causal_replay_case" else "CLEAN_CONTROL"
    _require(
        record.get("arm_eligibility") == _arm_eligibility(case_kind=case_kind),
        "pre_gold_arm_eligibility_mismatch",
    )


def _validate_prepared(
    cases: Sequence[Mapping[str, Any]],
    controls: Sequence[Mapping[str, Any]],
    ledger: Sequence[Mapping[str, Any]],
    arms: Mapping[str, Any],
) -> dict[str, Any]:
    for record in [*cases, *controls]:
        _validate_pre_gold_unit_identity(record)
    case_future_leakage = [record for record in cases if _forbidden_projection_paths(record)]
    control_future_leakage = [record for record in controls if _forbidden_projection_paths(record)]
    ledger_index = {
        (
            record.get("source_key"),
            record.get("task_run_id"),
            record.get("target_step"),
            record.get("candidate_id"),
        ): record
        for record in ledger
    }

    def ledger_matches(
        record: Mapping[str, Any],
        *,
        candidate_id: str,
        candidate_sha256: str,
        expected_disposition: str,
    ) -> bool:
        indexed = ledger_index.get(
            (
                record.get("source_key"),
                record.get("task", {}).get("task_run_id"),
                record.get("decision", {}).get("target_step"),
                candidate_id,
            )
        )
        return bool(
            indexed
            and indexed.get("candidate_sha256") == candidate_sha256
            and indexed.get("disposition") == expected_disposition
            and indexed.get("unit_kind")
            == (
                "STRICT_MHR"
                if record.get("record_type") == "causal_replay_case"
                else "CLEAN_CONTROL"
            )
            and indexed.get("unit_id") == record.get("case_id", record.get("control_id"))
            and indexed.get("model_id") == record.get("model_id")
            and indexed.get("model_config_manifest_sha256")
            == record.get("model_config_manifest_sha256")
            and indexed.get("model_config_record_sha256")
            == record.get("model_config_record_sha256")
        )

    checks = {
        "deployment_prediction_false": all(
            record.get("deployment_prediction") is False
            for record in [*cases, *controls, *ledger, arms]
        ),
        "curated_true": all(
            record.get("curated") is True for record in [*cases, *controls, *ledger, arms]
        ),
        "all_cases_candidate_frozen": all(
            record.get("case_status") == CASE_STATUS for record in cases
        ),
        "zero_included_cases": not any(record.get("case_status") == "INCLUDED" for record in cases),
        "all_controls_pre_gold": all(
            record.get("control_status") in {"SELECTED", "RESERVE"}
            and "admission_status" not in record
            for record in controls
        ),
        "zero_included_controls": not any("admission_status" in record for record in controls),
        "action_gold_channel_pending": all(
            record.get("action_gold_refs", {}).get("accepted_next_action_set_ref") is None
            and record.get("action_gold_refs", {}).get("curation_phase") == "G1_6_PENDING"
            for record in cases
        ),
        "transformation_channel_pending": all(
            record.get("transformation_refs", {}).get("curation_phase") == "G1_6_PENDING"
            for record in cases
        ),
        "pending_evidence_channels_valid": all(
            _pending_evidence_channels_valid(record) for record in [*cases, *controls]
        ),
        "unit_model_config_cross_bound": all(
            record.get("model_config_manifest_sha256") == MODEL_CONFIG_MANIFEST_SHA256
            and record.get("frozen_capsule", {}).get("model_config")
            == {
                "manifest_sha256": record.get("model_config_manifest_sha256"),
                "record_sha256": record.get("model_config_record_sha256"),
            }
            for record in [*cases, *controls]
        ),
        "ledger_model_config_and_unit_cross_bound": all(
            record.get("model_config_manifest_sha256") == MODEL_CONFIG_MANIFEST_SHA256
            and record.get("model_config_record_sha256")
            == _MODEL_CONFIG_RECORD_SHA256.get(record.get("model_id"))
            and (
                record.get("disposition") != "EXCLUDED"
                or (record.get("unit_kind") is None and record.get("unit_id") is None)
            )
            for record in ledger
        ),
        "zero_forbidden_post_target_refs": not case_future_leakage and not control_future_leakage,
        "run_ready_false": all(
            not eligibility.get("execution_ready")
            for record in [*cases, *controls]
            for eligibility in record.get("arm_eligibility", [])
        ),
        "admission_ready_false": True,
        "execution_ready_false": True,
        "treatment_response_generation_disallowed": True,
        "case_ids_unique": len({record.get("case_id") for record in cases}) == len(cases),
        "case_decisions_unique": len(
            {
                (
                    record.get("model_id"),
                    record.get("task", {}).get("task_run_id"),
                    record.get("decision", {}).get("target_step"),
                    record.get("decision", {}).get("request_event_id"),
                    record.get("frozen_capsule", {}).get("request_view_sha256"),
                )
                for record in cases
            }
        )
        == len(cases),
        "clean_pool_separate_from_case_census": not any("control_id" in record for record in cases),
        "strict_cases_have_focal_members": all(
            bool(record.get("eligibility_only_refs", {}).get("members")) for record in cases
        ),
        "strict_cases_cross_bound_to_ledger": all(
            ledger_matches(
                record,
                candidate_id=member.get("candidate_id", ""),
                candidate_sha256=member.get("candidate_sha256", ""),
                expected_disposition="CANDIDATE_FROZEN",
            )
            for record in cases
            for member in record.get("eligibility_only_refs", {}).get("members", [])
        ),
        "clean_controls_cross_bound_to_ledger": all(
            ledger_matches(
                record,
                candidate_id=record.get("eligibility_only_refs", {}).get("candidate_id", ""),
                candidate_sha256=record.get("eligibility_only_refs", {}).get(
                    "candidate_sha256", ""
                ),
                expected_disposition=(
                    "CLEAN_CONTROL_SELECTED"
                    if record.get("control_status") == "SELECTED"
                    else "CLEAN_CONTROL_RESERVE"
                ),
            )
            for record in controls
        ),
        "zero_unresolved_source_refs": True,
        "zero_unresolved_capsule_refs": True,
        "emitted_schema_validation_passed": True,
    }
    _require(all(checks.values()), "prepared_registry_invalid", checks=checks)
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "record_type": "causal_replay_dry_validation",
        "protocol_version": PROTOCOL_VERSION,
        "curated": True,
        "deployment_prediction": False,
        "valid": True,
        "pre_gold_status": {
            "gold_validation_status": "NOT_APPLICABLE_PRE_G1_6",
            "curation_and_admission_sealed": False,
            "admission_ready": False,
            "execution_ready": False,
            "run_ready": False,
            "treatment_response_generation_allowed": False,
            "pre_gold_pending_case_count": len(cases),
            "run_ready_case_count": 0,
            "unresolved_source_ref_count": 0,
            "unresolved_capsule_ref_count": 0,
            "pre_gold_future_leakage_case_count": len(case_future_leakage),
            "pre_gold_future_leakage_control_count": len(control_future_leakage),
        },
        "checks": checks,
        "counts": {
            "strict_mhr_cases": len(cases),
            "clean_controls": len(controls),
            "ledger_records": len(ledger),
            "included_cases": 0,
            "pre_gold_pending_cases": len(cases),
            "pre_gold_future_leakage_cases": len(case_future_leakage),
            "pre_gold_future_leakage_controls": len(control_future_leakage),
            "treatment_response_count": 0,
        },
    }


def _validate_emitted_schemas(artifacts: Mapping[str, Any]) -> None:
    schema_root = (
        Path(__file__).resolve().parents[4] / "mobileworld_audit_handoff" / "schemas" / "g1"
    )
    _require(schema_root.is_dir(), "g1_schema_root_missing", path=str(schema_root))
    schemas = {
        path.name: _load_json_object(path) for path in sorted(schema_root.glob("*.schema.json"))
    }
    required = {
        "case.schema.json",
        "clean_control.schema.json",
        "ledger.schema.json",
        "arm_catalog.schema.json",
        "registry_manifest.schema.json",
        "validation.schema.json",
    }
    _require(required <= set(schemas), "g1_schema_set_incomplete")
    base_uri = schema_root.as_uri() + "/"
    store: dict[str, Any] = {}
    for name, schema in schemas.items():
        store[name] = schema
        store[base_uri + name] = schema
    groups = {
        "case.schema.json": artifacts["cases"],
        "clean_control.schema.json": artifacts["clean_controls"],
        "ledger.schema.json": artifacts["ledger"],
        "arm_catalog.schema.json": [artifacts["arm_catalog"]],
        "registry_manifest.schema.json": [artifacts["manifest"]],
        "validation.schema.json": [artifacts["validation"]],
    }
    for schema_name, records in groups.items():
        schema = schemas[schema_name]
        try:
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(
                schema,
                resolver=RefResolver(base_uri=base_uri, referrer=schema, store=store),
            )
            for offset, record in enumerate(records):
                errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
                _require(
                    not errors,
                    "emitted_schema_validation_failed",
                    schema=schema_name,
                    record_offset=offset,
                    errors=[error.message for error in errors[:20]],
                )
        except CausalReplayRegistryError:
            raise
        except Exception as error:
            raise CausalReplayRegistryError(
                "schema_validator_failure", str(error), schema=schema_name
            ) from error


def validate_case_record(
    record: Mapping[str, Any], *, evidence_root: str | os.PathLike[str] | None = None
) -> None:
    """Validate one immutable G1.1 pre-gold record.

    G1.6 does not rewrite this record.  Inclusion/exclusion is represented by
    a separate append-only admission record validated by
    :func:`validate_admission_record`.
    """

    _require(evidence_root is None, "pre_gold_evidence_root_forbidden")
    _require(record.get("schema_version") == CASE_SCHEMA_VERSION, "case_schema_version")
    _validate_pre_gold_unit_identity(record)
    _require(record.get("curated") is True, "curated_must_be_true")
    _require(record.get("deployment_prediction") is False, "deployment_prediction_must_be_false")
    status = record.get("case_status")
    _require(status == CASE_STATUS, "pre_gold_case_status_invalid")
    eligibility = record.get("eligibility_only_refs")
    gold = record.get("action_gold_refs")
    transformations = record.get("transformation_refs")
    _require(isinstance(eligibility, Mapping), "eligibility_channel_invalid")
    _require(isinstance(gold, Mapping), "gold_action_channel_invalid")
    _require(isinstance(transformations, Mapping), "transformation_channel_invalid")
    _require(_pending_evidence_channels_valid(record), "pre_gold_channels_not_pending")


def validate_admission_record(
    record: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    evidence_root: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
    registry_manifest_sha256: str,
) -> None:
    """Validate one append-only G1.6 admission record and all referenced bytes.

    This is deliberately separate from :func:`validate_case_record`: a G1.6
    disposition never mutates the immutable G1.1 registry line.
    """

    _validate_static_schema("admission.schema.json", record)
    root = _resolve_base(evidence_root)
    raw_base = _resolve_base(source_base)
    expected_registry_sha = _sha(registry_manifest_sha256, "registry_manifest_sha256")
    unit_ref = record["unit_ref"]
    expected_unit = _expected_unit_ref(
        frozen_record,
        registry_manifest_sha256=expected_registry_sha,
    )
    _require(unit_ref == expected_unit, "admission_unit_ref_mismatch")

    referenced: dict[str, Mapping[str, Any]] = {}
    scalar_refs = {
        "action_gold_bundle_ref": "action_gold_bundle.schema.json",
        "action_gold_review_ledger_ref": "review_ledger.schema.json",
        "transformation_plan_ref": "transformation_plan.schema.json",
        "transformation_review_ledger_ref": "review_ledger.schema.json",
    }
    for field, schema_name in scalar_refs.items():
        reference = record.get(field)
        if reference is not None:
            referenced[field] = _load_schema_bound_ref(root, reference, schema_name=schema_name)
    arm_plans: dict[str, Mapping[str, Any]] = {}
    for arm_id, reference in record["arm_plan_refs"].items():
        if reference is not None:
            arm_plans[arm_id] = _load_schema_bound_ref(
                root,
                reference,
                schema_name="arm.schema.json",
            )
    validation_receipt = _load_schema_bound_ref(
        root,
        record["validation_receipt_ref"],
        schema_name="admission_validation.schema.json",
    )
    unit_id = expected_unit["unit_id"]
    _require(
        validation_receipt.get("admission_id") == record.get("admission_id")
        and validation_receipt.get("unit_id") == unit_id
        and validation_receipt.get("admission_status") == record.get("admission_status")
        and validation_receipt.get("reason_codes") == record.get("reason_codes"),
        "admission_validation_receipt_mismatch",
    )

    if record["admission_status"] == "EXCLUDED":
        _validate_excluded_admission(
            root,
            raw_base,
            record,
            frozen_record=frozen_record,
            expected_unit=expected_unit,
            referenced=referenced,
            arm_plans=arm_plans,
            validation_receipt=validation_receipt,
        )
        return

    _validate_source_registry_record_ref(root, record["source_registry_record_ref"], frozen_record)
    _require(
        validation_receipt.get("validation_result") == "INCLUDED_VALIDATED"
        and validation_receipt.get("mechanical_failure_evidence") == []
        and validation_receipt.get("checks")
        == {
            "all_refs_hash_resolved": True,
            "payload_schemas_valid": True,
            "evidence_cutoff_valid": True,
            "review_ledgers_valid": True,
            "transformations_valid": True,
            "arm_plans_valid": True,
            "future_evidence_leakage_zero": True,
            "treatment_response_count_zero": True,
            "exclusion_reason_valid": "NOT_APPLICABLE",
        },
        "included_validation_receipt_contract_mismatch",
    )
    required_payloads = set(scalar_refs)
    _require(required_payloads == set(referenced), "included_bundle_missing")
    gold = referenced["action_gold_bundle_ref"]
    action_review = referenced["action_gold_review_ledger_ref"]
    transformation = referenced["transformation_plan_ref"]
    transformation_review = referenced["transformation_review_ledger_ref"]
    for name, payload in referenced.items():
        if "review_ledger" not in name:
            _require(payload.get("unit_ref") == expected_unit, "included_payload_unit_mismatch")

    _validate_gold_bundle(
        root,
        raw_base,
        gold,
        frozen_record=frozen_record,
        expected_unit=expected_unit,
    )
    _validate_transformation_bundle(
        root,
        raw_base,
        transformation,
        frozen_record=frozen_record,
        expected_unit=expected_unit,
    )
    action_identities = _validate_review_ledger(
        root,
        raw_base,
        action_review,
        frozen_record=frozen_record,
        expected_channel="ACTION_GOLD",
        expected_payload_ref=record["action_gold_bundle_ref"],
        expected_input_ref=gold["curation_input_manifest_ref"],
        expected_unit=expected_unit,
    )
    transformation_identities = _validate_review_ledger(
        root,
        raw_base,
        transformation_review,
        frozen_record=frozen_record,
        expected_channel="TRANSFORMATION",
        expected_payload_ref=record["transformation_plan_ref"],
        expected_input_ref=transformation["curation_input_manifest_ref"],
        expected_unit=expected_unit,
    )
    _require(
        set(action_identities).isdisjoint(transformation_identities),
        "curator_identity_not_disjoint",
    )
    _require(
        record["review_identity_sets"]
        == {"action_gold": action_identities, "transformation": transformation_identities},
        "admission_review_identity_set_mismatch",
    )
    _validate_admission_arm_plans(
        root,
        record,
        arm_plans,
        frozen_record=frozen_record,
        transformation=transformation,
        action_gold=gold,
    )


def _expected_excluded_validation_checks(reason_codes: Sequence[str]) -> dict[str, Any]:
    reasons = frozenset(reason_codes)
    has_curator_evidence = bool(reasons & _CURATOR_EXCLUSION_CHANNEL_BY_REASON.keys())
    has_future_leakage = "FUTURE_EVIDENCE_LEAKAGE" in reasons
    return {
        "all_refs_hash_resolved": "SOURCE_REFERENCE_UNRESOLVED" not in reasons,
        "payload_schemas_valid": True,
        "evidence_cutoff_valid": (
            False if has_future_leakage else True if has_curator_evidence else "NOT_APPLICABLE"
        ),
        "review_ledgers_valid": True if has_curator_evidence else "NOT_APPLICABLE",
        "transformations_valid": (
            False if "TARGET_SPAN_UNRESOLVED" in reasons else "NOT_APPLICABLE"
        ),
        "arm_plans_valid": False if "ARM_PROTOCOL_INVALID" in reasons else "NOT_APPLICABLE",
        "future_evidence_leakage_zero": (
            False if has_future_leakage else True if has_curator_evidence else "NOT_APPLICABLE"
        ),
        "treatment_response_count_zero": True,
        "exclusion_reason_valid": True,
    }


def _validate_excluded_admission(
    root: Path,
    source_base: Path,
    record: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any]],
    arm_plans: Mapping[str, Mapping[str, Any]],
    validation_receipt: Mapping[str, Any],
) -> None:
    """Close every G1.6 exclusion with review or replayed mechanical evidence."""

    reasons = record["reason_codes"]
    _require(
        reasons == sorted(reasons) and set(reasons) <= EXCLUSION_REASON_CODES and bool(reasons),
        "admission_exclusion_reason_not_frozen",
    )
    _require(
        expected_unit["unit_kind"] == "STRICT_MHR"
        or not (set(reasons) & _STRICT_ONLY_EXCLUSION_REASONS),
        "clean_control_strict_mhr_exclusion_reason_forbidden",
    )
    _require(
        validation_receipt.get("validation_result") == "EXCLUSION_EVIDENCE_VALIDATED"
        and validation_receipt.get("checks") == _expected_excluded_validation_checks(reasons),
        "excluded_validation_receipt_contract_mismatch",
    )

    mechanical_reasons = sorted(
        reason for reason in reasons if reason in _MECHANICAL_EXCLUSION_VALIDATOR_BY_REASON
    )
    mechanical_evidence = validation_receipt.get("mechanical_failure_evidence")
    _require(
        isinstance(mechanical_evidence, list)
        and [entry.get("reason_code") for entry in mechanical_evidence] == mechanical_reasons,
        "mechanical_exclusion_evidence_coverage_mismatch",
    )

    if "SOURCE_REFERENCE_UNRESOLVED" not in reasons:
        _validate_source_registry_record_ref(
            root,
            record["source_registry_record_ref"],
            frozen_record,
        )

    curator_reasons = [
        reason for reason in reasons if reason in _CURATOR_EXCLUSION_CHANNEL_BY_REASON
    ]
    action_reasons = [
        reason
        for reason in curator_reasons
        if _CURATOR_EXCLUSION_CHANNEL_BY_REASON[reason] == "ACTION_GOLD"
    ]
    transformation_reasons = [
        reason
        for reason in curator_reasons
        if _CURATOR_EXCLUSION_CHANNEL_BY_REASON[reason] == "TRANSFORMATION"
    ]
    _require(
        len(action_reasons) <= 1 and len(transformation_reasons) <= 1,
        "curator_exclusion_reason_not_individually_covered",
    )
    identity_sets: dict[str, list[str]] = {"action_gold": [], "transformation": []}
    channel_contract = (
        (
            "action_gold",
            action_reasons,
            "action_gold_review_ledger_ref",
            "action_gold_bundle_ref",
            "ACTION_GOLD",
        ),
        (
            "transformation",
            transformation_reasons,
            "transformation_review_ledger_ref",
            "transformation_plan_ref",
            "TRANSFORMATION",
        ),
    )
    for identity_key, channel_reasons, ledger_field, payload_field, channel in channel_contract:
        ledger = referenced.get(ledger_field)
        if not channel_reasons:
            _require(ledger is None, "unjustified_exclusion_review_ledger_present", channel=channel)
            continue
        _require(
            ledger is not None and referenced.get(payload_field) is None,
            "curator_exclusion_channel_artifact_mismatch",
            channel=channel,
        )
        reason = channel_reasons[0]
        identity_sets[identity_key] = _validate_review_ledger(
            root,
            source_base,
            ledger,
            frozen_record=frozen_record,
            expected_channel=channel,
            expected_payload_ref=None,
            expected_input_ref=ledger["curation_input_manifest_ref"],
            expected_unit=expected_unit,
            expected_disposition="EXCLUDE",
            expected_exclusion_reason=reason,
        )

    _require(
        set(identity_sets["action_gold"]).isdisjoint(identity_sets["transformation"]),
        "curator_identity_not_disjoint",
    )
    _require(
        record["review_identity_sets"] == identity_sets,
        "admission_review_identity_set_mismatch",
    )

    allowed_payload_reasons = {
        "action_gold_bundle_ref": {"FUTURE_EVIDENCE_LEAKAGE", "ARM_PROTOCOL_INVALID"},
        "transformation_plan_ref": {
            "TARGET_SPAN_UNRESOLVED",
            "FUTURE_EVIDENCE_LEAKAGE",
            "ARM_PROTOCOL_INVALID",
        },
    }
    reason_set = set(reasons)
    for field, allowed_reasons in allowed_payload_reasons.items():
        payload = referenced.get(field)
        if payload is not None:
            _require(
                bool(reason_set & allowed_reasons) and payload.get("unit_ref") == expected_unit,
                "unjustified_exclusion_payload_present",
                field=field,
            )
    if "ARM_PROTOCOL_INVALID" not in reason_set:
        _require(not arm_plans, "unjustified_exclusion_arm_plan_present")

    by_reason = {entry["reason_code"]: entry for entry in mechanical_evidence}
    for reason in mechanical_reasons:
        expected_validator, actual_failure = _replay_mechanical_exclusion(
            reason,
            root=root,
            source_base=source_base,
            record=record,
            frozen_record=frozen_record,
            expected_unit=expected_unit,
            referenced=referenced,
            arm_plans=arm_plans,
        )
        _validate_mechanical_failure_receipt(
            by_reason[reason],
            reason=reason,
            expected_validator=expected_validator,
            actual_failure=actual_failure,
        )


def _validate_mechanical_failure_receipt(
    evidence: Mapping[str, Any],
    *,
    reason: str,
    expected_validator: str,
    actual_failure: str,
) -> None:
    _require(
        evidence.get("reason_code") == reason
        and evidence.get("validator_id") == expected_validator
        and evidence.get("validator_failure_code") == actual_failure,
        "mechanical_exclusion_failure_receipt_mismatch",
        reason=reason,
        actual_failure=actual_failure,
    )


def _capture_mechanical_failure(
    reason: str,
    validator: Any,
    *,
    allowed_code: Any = None,
) -> tuple[str, str]:
    validator_id = _MECHANICAL_EXCLUSION_VALIDATOR_BY_REASON[reason]
    try:
        validator()
    except CausalReplayRegistryError as error:
        allowed = (
            allowed_code(error.code)
            if callable(allowed_code)
            else allowed_code is None or error.code in allowed_code
        )
        _require(
            allowed,
            "mechanical_exclusion_failure_not_reason_specific",
            reason=reason,
            failure_code=error.code,
        )
        return validator_id, error.code
    raise CausalReplayRegistryError(
        "mechanical_exclusion_not_reproduced",
        "mechanical exclusion not reproduced",
        reason=reason,
    )


def _replay_mechanical_exclusion(
    reason: str,
    *,
    root: Path,
    source_base: Path,
    record: Mapping[str, Any],
    frozen_record: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any]],
    arm_plans: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    if reason == "SOURCE_REFERENCE_UNRESOLVED":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_source_references(
                root,
                source_base,
                record,
                frozen_record,
            ),
        )
    if reason == "REQUEST_HASH_MISMATCH":
        return _capture_mechanical_failure(
            reason,
            lambda: _request_view_for_frozen_unit(source_base, frozen_record),
            allowed_code={"admission_request_view_hash_mismatch"},
        )
    if reason == "STATE_HASH_MISMATCH":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_current_gui(source_base, frozen_record),
            allowed_code={"admission_current_gui_hash_mismatch"},
        )
    if reason == "TARGET_SPAN_UNRESOLVED":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_target_spans(source_base, frozen_record),
            allowed_code=lambda code: code.startswith("admission_target_span_")
            or code == "admission_target_record_hash_mismatch",
        )
    if reason in {
        "PROVENANCE_BELOW_HIGH",
        "NOT_REFUTED_OR_STALE",
        "NO_EXPLICIT_UPTAKE",
        "NOT_STRICT_MHR",
    }:
        expected_code = {
            "PROVENANCE_BELOW_HIGH": "admission_provenance_below_high",
            "NOT_REFUTED_OR_STALE": "admission_not_refuted_or_stale",
            "NO_EXPLICIT_UPTAKE": "admission_no_explicit_uptake",
            "NOT_STRICT_MHR": "admission_not_strict_mhr",
        }[reason]
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_strict_gate(frozen_record, reason),
            allowed_code={expected_code},
        )
    if reason == "ORIGINAL_ACTION_UNPARSEABLE":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_original_action(
                frozen_record["frozen_capsule"]["original_action"]["parsed_action"],
                {"parse_outcome": "returned", "parse_exception": None},
            ),
            allowed_code={
                "original_action_unparseable",
                "parsed_action_invalid",
                "parsed_action_value_invalid",
                "parsed_action_placeholder_forbidden",
            },
        )
    if reason == "BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_backend_checkpoint(frozen_record),
            allowed_code={"admission_backend_checkpoint_required_but_missing"},
        )
    if reason == "FUTURE_EVIDENCE_LEAKAGE":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_admission_future_evidence(
                root,
                source_base,
                referenced,
                frozen_record=frozen_record,
                expected_unit=expected_unit,
            ),
            allowed_code=_FUTURE_EVIDENCE_FAILURE_CODES,
        )
    if reason == "ARM_PROTOCOL_INVALID":
        return _capture_mechanical_failure(
            reason,
            lambda: _validate_excluded_arm_protocol(
                root,
                record,
                frozen_record=frozen_record,
                referenced=referenced,
                arm_plans=arm_plans,
            ),
            allowed_code=lambda code: code != "arm_protocol_required_bundle_missing"
            and code.startswith(("arm_", "included_arm_")),
        )
    _require(reason == "DUPLICATE_CAPSULE", "mechanical_exclusion_reason_unhandled")
    return _capture_mechanical_failure(
        reason,
        lambda: _validate_duplicate_capsule_exclusion(root, record, frozen_record),
        allowed_code={"admission_duplicate_capsule"},
    )


def _validate_admission_source_references(
    root: Path,
    source_base: Path,
    record: Mapping[str, Any],
    frozen_record: Mapping[str, Any],
) -> None:
    _validate_source_registry_record_ref(
        root,
        record["source_registry_record_ref"],
        frozen_record,
    )
    _raw_events_for_frozen_unit(source_base, frozen_record)


def _validate_admission_current_gui(source_base: Path, frozen_record: Mapping[str, Any]) -> None:
    events = _raw_events_for_frozen_unit(source_base, frozen_record)
    target_step = frozen_record["decision"]["target_step"]
    candidates = [
        event
        for event in events.values()
        if event.get("event_type") == "step_started"
        and event.get("payload", {}).get("step_index") == target_step
    ]
    _require(len(candidates) == 1, "admission_current_gui_hash_mismatch")
    actual = _current_gui_ref(candidates[0]["payload"].get("observation"))
    _require(
        actual.get("digest") == frozen_record["frozen_capsule"]["current_gui_blob"]["digest"],
        "admission_current_gui_hash_mismatch",
    )


def _validate_admission_target_spans(source_base: Path, frozen_record: Mapping[str, Any]) -> None:
    request_view = _request_view_for_frozen_unit(source_base, frozen_record)
    for target in frozen_record["target_histories"]:
        text = _request_record_text(request_view, target["request_path"])
        _require(
            _digest(text.encode("utf-8")) == target["source_record_sha256"],
            "admission_target_record_hash_mismatch",
        )
        if target["edit_span_status"] == "G1_1_FROZEN":
            for span in target["focal_edit_spans"]:
                _validate_exact_span(text, span, code="admission_target_span")
        else:
            _require(
                target["edit_span_status"] == "G1_6_PENDING" and target["focal_edit_spans"] == [],
                "admission_target_span_pending_contract_invalid",
            )


def _validate_admission_strict_gate(frozen_record: Mapping[str, Any], reason: str) -> None:
    facts = frozen_record.get("eligibility_only_refs", {}).get("facts", {})
    _require(isinstance(facts, Mapping), "admission_not_strict_mhr")
    if reason == "PROVENANCE_BELOW_HIGH":
        _require(facts.get("provenance_exact_or_high") is True, "admission_provenance_below_high")
    elif reason == "NOT_REFUTED_OR_STALE":
        _require(
            facts.get("validity_refuted_or_stale") is True,
            "admission_not_refuted_or_stale",
        )
    elif reason == "NO_EXPLICIT_UPTAKE":
        _require(facts.get("explicit_use") is True, "admission_no_explicit_uptake")
    else:
        _require(reason == "NOT_STRICT_MHR", "admission_strict_gate_reason_invalid")
        _require(
            all(
                facts.get(field) is True
                for field in ("coverage_complete", "actual_exposure", "low_state_confound")
            ),
            "admission_not_strict_mhr",
        )


def _validate_admission_backend_checkpoint(frozen_record: Mapping[str, Any]) -> None:
    backend = frozen_record.get("frozen_capsule", {}).get("backend_checkpoint", {})
    _require(
        not (backend.get("required") is True and backend.get("reference") is None),
        "admission_backend_checkpoint_required_but_missing",
    )


def _validate_admission_future_evidence(
    root: Path,
    source_base: Path,
    referenced: Mapping[str, Mapping[str, Any]],
    *,
    frozen_record: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
) -> None:
    input_refs: dict[str, Mapping[str, Any]] = {}
    for payload in referenced.values():
        reference = payload.get("curation_input_manifest_ref")
        if isinstance(reference, Mapping):
            input_refs[reference["sha256"]] = reference
    _require(bool(input_refs), "admission_future_evidence_not_present")
    for reference in input_refs.values():
        manifest = _load_canonical_ref_object(root, reference)
        forbidden = _forbidden_projection_paths(manifest)
        _require(
            not forbidden,
            "admission_future_evidence_projection_forbidden",
            paths=forbidden,
        )
        for evidence_ref in manifest.get("evidence_refs", []):
            artifact = _load_canonical_ref_object(root, evidence_ref)
            forbidden = _forbidden_projection_paths(artifact)
            _require(
                not forbidden,
                "admission_future_evidence_projection_forbidden",
                paths=forbidden,
            )
        _validate_curation_input_manifest(
            root,
            source_base,
            reference,
            expected_channel=manifest["channel"],
            expected_unit=expected_unit,
            expected_evidence_refs=manifest["evidence_refs"],
            frozen_record=frozen_record,
        )


def _validate_excluded_arm_protocol(
    root: Path,
    record: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any]],
    arm_plans: Mapping[str, Mapping[str, Any]],
) -> None:
    gold = referenced.get("action_gold_bundle_ref")
    transformation = referenced.get("transformation_plan_ref")
    _require(
        gold is not None and transformation is not None,
        "arm_protocol_required_bundle_missing",
    )
    _validate_admission_arm_plans(
        root,
        record,
        arm_plans,
        frozen_record=frozen_record,
        transformation=transformation,
        action_gold=gold,
    )


def _validate_duplicate_capsule_exclusion(
    root: Path,
    record: Mapping[str, Any],
    frozen_record: Mapping[str, Any],
) -> None:
    reference = record["source_registry_record_ref"]
    _validate_source_registry_record_ref(root, reference, frozen_record)
    path = _safe_child(
        root,
        _relative_path(reference["relative_path"], "source_registry_record_ref.relative_path"),
    )
    records = _load_canonical_jsonl_bytes(_read_regular(path), path)
    capsule_sha256 = frozen_record["frozen_capsule"]["capsule_sha256"]
    duplicates = [
        candidate
        for candidate in records
        if candidate.get("frozen_capsule", {}).get("capsule_sha256") == capsule_sha256
    ]
    _require(len(duplicates) == 1, "admission_duplicate_capsule")


def _arm_target_span(target: Mapping[str, Any], *, target_kind: str) -> dict[str, Any]:
    binding = target["record_binding"]
    edit = target["edit_span"]
    return {
        "ordinal": target["ordinal"],
        "target_id": target["target_id"],
        "target_kind": target_kind,
        "source_candidate_ids": target["source_candidate_ids"],
        "record_identity_sha256": binding["record_identity_sha256"],
        "request_path": binding["request_path"],
        "message_index": binding["message_index"],
        "content_item_index": binding["content_item_index"],
        "message_role": binding["message_role"],
        "content_item_kind": binding["content_item_kind"],
        "representation_record_class": binding["representation_record_class"],
        "record_sha256": binding["record_sha256"],
        "record_codepoint_count": binding["record_codepoint_count"],
        "record_utf8_byte_count": binding["record_utf8_byte_count"],
        "char_start": edit["char_start"],
        "char_end": edit["char_end"],
        "utf8_byte_start": edit["utf8_byte_start"],
        "utf8_byte_end": edit["utf8_byte_end"],
        "span_sha256": edit["span_sha256"],
        "location_bucket": _location_bucket(target),
    }


def _arm_delimiter_repair(repair: Mapping[str, Any]) -> dict[str, Any]:
    span = repair["deleted_syntax_span"]
    return {
        "repair_id": repair["repair_id"],
        "record_identity_sha256": repair["record_identity_sha256"],
        "operation": repair["operation"],
        "char_start": span["char_start"],
        "char_end": span["char_end"],
        "utf8_byte_start": span["utf8_byte_start"],
        "utf8_byte_end": span["utf8_byte_end"],
        "span_sha256": span["span_sha256"],
        "semantic_content_added": False,
    }


def _expected_transformation_invariants(unit_kind: str) -> dict[str, bool | str]:
    strict_value: bool | str = True if unit_kind == "STRICT_MHR" else "NOT_APPLICABLE"
    clean_value: bool | str = "NOT_APPLICABLE" if unit_kind == "STRICT_MHR" else True
    return {
        "targets_ordered_unique_nonoverlapping": True,
        **{name: strict_value for name in _STRICT_TRANSFORMATION_INVARIANTS},
        "sham_nonoverlapping_with_misleading_targets": True,
        "protected_spans_untouched": True,
        "future_evidence_leakage_zero": True,
        "only_original_and_sham_applicable": clean_value,
    }


def _expected_arm_invariants(unit_kind: str) -> dict[str, bool | str]:
    strict_value: bool | str = True if unit_kind == "STRICT_MHR" else "NOT_APPLICABLE"
    clean_value: bool | str = "NOT_APPLICABLE" if unit_kind == "STRICT_MHR" else True
    return {
        "targets_ordered_unique_nonoverlapping": True,
        "selected_set_hash_verified": True,
        "mask_targets_equal_focal": strict_value,
        "mask_correction_targets_equal_focal": strict_value,
        "oracle_targets_equal_oracle_set": strict_value,
        "oracle_is_focal_superset": strict_value,
        "insertions_bind_deleted_targets": True,
        "delimiter_repairs_bind_source_syntax": True,
        "sham_binds_exactly_one_benign_and_one_focal": True,
        "sham_nonoverlapping_with_misleading_targets": True,
        "sham_token_counts_recomputed": True,
        "sham_location_buckets_recomputed": True,
        "only_original_and_sham_applicable": clean_value,
    }


def _validate_admission_arm_plans(
    root: Path,
    admission: Mapping[str, Any],
    plans: Mapping[str, Mapping[str, Any]],
    *,
    frozen_record: Mapping[str, Any],
    transformation: Mapping[str, Any],
    action_gold: Mapping[str, Any],
) -> None:
    """Cross-bind every executable arm to the sealed G1.6 artifacts."""

    applicable = admission["applicable_arms"]
    _require(applicable == transformation["applicable_arms"], "arm_applicability_mismatch")
    _require(set(plans) == set(applicable), "included_arm_plan_set_mismatch")
    unit_kind = admission["unit_ref"]["unit_kind"]
    unit_id = admission["unit_ref"]["unit_id"]
    expected_focal = [
        _arm_target_span(target, target_kind="FOCAL_MHR")
        for target in transformation["focal_target_set"]
    ]
    expected_oracle = [
        _arm_target_span(target, target_kind="ORACLE_RELEVANT_MHR")
        for target in transformation["oracle_target_set"]
    ]
    benign_target = _arm_target_span(
        transformation["sham_benign_edit"]["benign_target"],
        target_kind="BENIGN_SHAM",
    )
    expected_repairs = {
        arm_id: [_arm_delimiter_repair(repair) for repair in repairs]
        for arm_id, repairs in transformation["delimiter_repairs"].items()
    }
    expected_insertions: list[dict[str, Any]] = []
    for correction in transformation["corrections"]:
        text_artifact = _load_schema_bound_ref(
            root,
            correction["correction_utf8_ref"],
            schema_name="utf8_text.schema.json",
        )
        expected_insertions.append(
            {
                "target_id": correction["target_id"],
                "insertion_position": correction["insertion_position"],
                "correction_utf8_ref": correction["correction_utf8_ref"],
                "correction_sha256": text_artifact["text_utf8_sha256"],
                "token_count": correction["token_count"],
                "utf8_byte_count": correction["utf8_byte_count"],
                "codepoint_count": correction["codepoint_count"],
            }
        )

    common = {
        "unit_kind": unit_kind,
        "case_id": unit_id,
        "model_id": frozen_record["model_id"],
        "unit_record_sha256": admission["unit_ref"]["unit_record_sha256"],
        "frozen_capsule_sha256": admission["unit_ref"]["frozen_capsule_sha256"],
        "transformation_plan_sha256": admission["transformation_plan_ref"]["sha256"],
        "action_gold_bundle_sha256": admission["action_gold_bundle_ref"]["sha256"],
        "model_config_manifest_sha256": MODEL_CONFIG_MANIFEST_SHA256,
        "focal_target_set_sha256": transformation["focal_target_set_sha256"],
        "oracle_target_set_sha256": transformation["oracle_target_set_sha256"],
    }
    for arm_id in applicable:
        plan = plans[arm_id]
        _require(plan["arm_id"] == arm_id, "arm_plan_id_key_mismatch")
        _require(
            plan["invariants"] == _expected_arm_invariants(unit_kind),
            "arm_plan_invariants_mismatch",
        )
        for field, expected in common.items():
            _require(plan[field] == expected, "arm_plan_common_binding_mismatch", field=field)

        if arm_id == "ORIGINAL":
            expected_targets: list[dict[str, Any]] = []
            expected_selected_hash = None
            expected_arm_insertions: list[dict[str, Any]] = []
            expected_arm_repairs: list[dict[str, Any]] = []
        elif arm_id in {"MASK", "MASK_CORRECTION"}:
            expected_targets = expected_focal
            expected_selected_hash = transformation["focal_target_set_sha256"]
            expected_arm_insertions = expected_insertions if arm_id == "MASK_CORRECTION" else []
            expected_arm_repairs = expected_repairs[arm_id]
        elif arm_id == "ORACLE_CLEAN":
            expected_targets = expected_oracle
            expected_selected_hash = transformation["oracle_target_set_sha256"]
            expected_arm_insertions = []
            expected_arm_repairs = expected_repairs[arm_id]
        else:
            _require(arm_id == "SHAM_BENIGN_EDIT", "arm_id_unhandled")
            expected_targets = [benign_target]
            expected_selected_hash = canonical_sha256(
                [transformation["sham_benign_edit"]["benign_target"]]
            )
            expected_arm_insertions = []
            expected_arm_repairs = expected_repairs[arm_id]

        _require(plan["target_spans"] == expected_targets, "arm_target_set_mismatch")
        _require(
            plan["selected_target_set_sha256"] == expected_selected_hash,
            "arm_selected_target_set_hash_mismatch",
        )
        _require(plan["insertions"] == expected_arm_insertions, "arm_insertions_mismatch")
        _require(
            plan["delimiter_repairs"] == expected_arm_repairs,
            "arm_delimiter_repairs_mismatch",
        )

        if arm_id == "SHAM_BENIGN_EDIT":
            sham = transformation["sham_benign_edit"]
            focal_target = next(
                target
                for target in expected_focal
                if target["target_id"] == sham["matched_focal_target_id"]
            )
            location = sham["location_match"]
            expected_match = {
                "matched_focal_target": focal_target,
                "benign_target": benign_target,
                "tokenizer_binding": transformation["tokenizer_binding"],
                "focal_token_count": sham["focal_token_count"],
                "benign_token_count": sham["benign_token_count"],
                "absolute_token_difference": sham["absolute_token_difference"],
                "token_ratio_numerator": sham["benign_token_count"],
                "token_ratio_denominator": sham["focal_token_count"],
                "token_match_rule": sham["token_match_rule"],
                "focal_location_bucket": location["focal_bucket"],
                "benign_location_bucket": location["benign_bucket"],
                "same_request_record": location["same_request_record"],
                "focal_history_depth": location["focal_history_depth"],
                "benign_history_depth": location["benign_history_depth"],
                "history_depth_difference": location["history_depth_difference"],
                "same_record_unavailable_reviewed": location["same_record_unavailable_reviewed"],
                "semantic_review_ledger_sha256": admission["transformation_review_ledger_ref"][
                    "sha256"
                ],
            }
            _require(plan["sham_match"] == expected_match, "arm_sham_match_mismatch")
        else:
            _require(plan["sham_match"] is None, "non_sham_arm_has_sham_match")

    _require(action_gold["unit_ref"] == transformation["unit_ref"], "arm_bundle_unit_mismatch")


def _schema_root() -> Path:
    return Path(__file__).resolve().parents[4] / "mobileworld_audit_handoff" / "schemas" / "g1"


def _validate_static_schema(schema_name: str, value: Any) -> None:
    schema_root = _schema_root()
    schemas = {
        path.name: _load_json_object(path) for path in sorted(schema_root.glob("*.schema.json"))
    }
    _require(schema_name in schemas, "g1_schema_missing", schema=schema_name)
    schema = schemas[schema_name]
    base_uri = schema_root.as_uri() + "/"
    store: dict[str, Any] = {}
    for name, candidate in schemas.items():
        store[name] = candidate
        store[base_uri + name] = candidate
    errors = sorted(
        Draft202012Validator(
            schema,
            resolver=RefResolver(base_uri=base_uri, referrer=schema, store=store),
        ).iter_errors(value),
        key=lambda error: list(error.path),
    )
    _require(
        not errors,
        "g1_schema_validation_failed",
        schema=schema_name,
        errors=[error.message for error in errors[:20]],
    )


def _load_canonical_ref_object(root: Path, reference: Any) -> Mapping[str, Any]:
    _require(isinstance(reference, Mapping), "content_ref_invalid")
    relative = _relative_path(reference.get("relative_path"), "content_ref.relative_path")
    path = _safe_child(root, relative)
    data = _read_regular(path)
    _require(reference.get("byte_count") == len(data), "content_ref_byte_count_mismatch")
    _require(reference.get("sha256") == _digest(data), "content_ref_hash_mismatch")
    value = _load_json_bytes(data, path)
    _require(isinstance(value, Mapping), "content_ref_not_json_object")
    _require(data == canonical_json_bytes(value), "content_ref_not_canonical_json")
    declared = reference.get("schema_version")
    if declared is not None:
        _require(value.get("schema_version") == declared, "content_ref_schema_version_mismatch")
    return value


def _load_schema_bound_ref(
    root: Path,
    reference: Any,
    *,
    schema_name: str,
) -> Mapping[str, Any]:
    value = _load_canonical_ref_object(root, reference)
    _validate_static_schema(schema_name, value)
    return value


def _expected_unit_ref(
    frozen_record: Mapping[str, Any], *, registry_manifest_sha256: str
) -> dict[str, Any]:
    _validate_pre_gold_unit_identity(frozen_record)
    record_type = frozen_record.get("record_type")
    if record_type == "causal_replay_case":
        validate_case_record(frozen_record)
        unit_kind = "STRICT_MHR"
        unit_id = frozen_record.get("case_id")
    else:
        _require(record_type == "causal_replay_clean_control", "frozen_unit_record_type_invalid")
        _require(_pending_evidence_channels_valid(frozen_record), "frozen_control_channels_invalid")
        _require(
            frozen_record.get("control_status") == "SELECTED",
            "reserve_clean_control_not_admission_eligible",
        )
        unit_kind = "CLEAN_CONTROL"
        unit_id = frozen_record.get("control_id")
    task = frozen_record.get("task", {})
    decision = frozen_record.get("decision", {})
    capsule = frozen_record.get("frozen_capsule", {})
    cutoff = decision.get("request_cutoff", {})
    return {
        "unit_kind": unit_kind,
        "unit_id": unit_id,
        "unit_record_sha256": canonical_sha256(frozen_record),
        "source_registry_manifest_sha256": registry_manifest_sha256,
        "frozen_capsule_sha256": capsule.get("capsule_sha256"),
        "request_view_sha256": capsule.get("request_view_sha256"),
        "current_gui_sha256": capsule.get("current_gui_blob", {}).get("digest"),
        "task_instruction_sha256": task.get("task_instruction_sha256"),
        "task_parameters_sha256": task.get("task_parameters_sha256"),
        "request_cutoff_event_id": cutoff.get("event_id"),
        "request_cutoff_event_seq": cutoff.get("event_seq"),
        "target_step": decision.get("target_step"),
        "model_id": frozen_record.get("model_id"),
        "history_family": frozen_record.get("history_family"),
    }


def _validate_source_registry_record_ref(
    root: Path, reference: Mapping[str, Any], frozen_record: Mapping[str, Any]
) -> None:
    relative = _relative_path(reference.get("relative_path"), "source_registry_record_ref")
    path = _safe_child(root, relative)
    data = _read_regular(path)
    _require(reference.get("file_sha256") == _digest(data), "source_registry_file_hash_mismatch")
    _require(reference.get("file_byte_count") == len(data), "source_registry_file_size_mismatch")
    records = _load_canonical_jsonl_bytes(data, path)
    index = _nonnegative_int(reference.get("record_index"), "source_registry_record_index")
    _require(index < len(records), "source_registry_record_index_out_of_bounds")
    actual = records[index]
    unit_id = frozen_record.get("case_id", frozen_record.get("control_id"))
    _require(actual == frozen_record, "source_registry_record_bytes_mismatch")
    _require(reference.get("record_id") == unit_id, "source_registry_record_id_mismatch")
    _require(
        reference.get("record_sha256") == canonical_sha256(actual),
        "source_registry_record_hash_mismatch",
    )


def _raw_events_for_frozen_unit(
    source_base: Path, frozen_record: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    locator = frozen_record["frozen_capsule"]["source_locator"]
    run_root = _resolve_input(source_base, locator["source_relative_run_path"])
    stream_path = _safe_child(run_root, locator["task_stream_relative_path"])
    data = _read_regular(stream_path)
    _require(_digest(data) == locator["task_stream_sha256"], "admission_task_stream_drift")
    events = _load_jsonl_prefix_through_event(
        data,
        stream_path,
        stop_event_id=frozen_record["decision"]["request_event_id"],
    )
    by_id = {event.get("event_id"): event for event in events}
    _require(len(by_id) == len(events) and None not in by_id, "admission_event_id_invalid")
    return by_id


def _validate_evidence_refs(
    root: Path,
    source_base: Path,
    references: Any,
    *,
    frozen_record: Mapping[str, Any],
    allowed_roles: frozenset[str],
) -> None:
    _require(isinstance(references, list) and references, "curation_evidence_refs_invalid")
    events = _raw_events_for_frozen_unit(source_base, frozen_record)
    step_by_id = {
        event.get("payload", {}).get("step_id"): event.get("payload", {}).get("step_index")
        for event in events.values()
        if event.get("event_type") == "step_started"
    }
    cutoff = frozen_record["decision"]["request_cutoff"]
    target_step = frozen_record["decision"]["target_step"]
    target_request = _event(
        events,
        frozen_record["decision"]["request_event_id"],
        "model_request",
    )
    _require(
        target_request.get("seq") == cutoff["event_seq"],
        "curation_target_request_cutoff_mismatch",
    )
    ref_ids: set[str] = set()
    digests: set[str] = set()
    relative_paths: set[str] = set()
    order_keys: list[tuple[str, int, int, str]] = []
    for index, reference in enumerate(references):
        _require(isinstance(reference, Mapping), "curation_evidence_ref_invalid", index=index)
        role = reference.get("evidence_role")
        _require(role in allowed_roles, "curation_evidence_role_forbidden", role=role)
        relative = _relative_path(reference.get("relative_path"), f"evidence_refs[{index}]")
        _reject_forbidden_input_name(relative, path=f"evidence_refs[{index}].relative_path")
        data = _read_regular(_safe_child(root, relative))
        digest = _sha(reference.get("sha256"), f"evidence_refs[{index}].sha256")
        _require(_digest(data) == digest, "curation_evidence_hash_mismatch")
        _require(reference.get("byte_count") == len(data), "curation_evidence_size_mismatch")
        artifact = _load_json_bytes(data, _safe_child(root, relative))
        _require(isinstance(artifact, Mapping), "curation_evidence_artifact_invalid")
        _require(data == canonical_json_bytes(artifact), "curation_evidence_not_canonical")
        _validate_static_schema("curation_evidence.schema.json", artifact)
        _require(
            reference.get("artifact_schema_version") == artifact.get("schema_version")
            and reference.get("ref_id") == artifact.get("ref_id")
            and role == artifact.get("evidence_role")
            and reference.get("projection_path") == artifact.get("projection_path")
            and reference.get("event_id") == artifact.get("source_event_id")
            and reference.get("event_seq") == artifact.get("source_event_seq")
            and reference.get("observed_step") == artifact.get("observed_step"),
            "curation_evidence_ref_artifact_mismatch",
        )
        ref_id = _string(reference.get("ref_id"), "evidence.ref_id")
        _require(ref_id not in ref_ids, "curation_evidence_ref_id_duplicate")
        _require(digest not in digests, "curation_evidence_digest_duplicate")
        _require(relative not in relative_paths, "curation_evidence_path_duplicate")
        ref_ids.add(ref_id)
        digests.add(digest)
        relative_paths.add(relative)
        observed_step = _nonnegative_int(reference.get("observed_step"), "observed_step")
        _require(observed_step <= target_step, "curation_evidence_after_target_step")
        event_id = reference.get("event_id")
        event_seq = reference.get("event_seq")
        order_keys.append(
            (
                _string(role, "evidence.evidence_role"),
                observed_step,
                -1 if event_seq is None else _nonnegative_int(event_seq, "event_seq"),
                ref_id,
            )
        )
        if role == "task_instruction":
            _require(
                event_id is None and event_seq is None and observed_step == 0,
                "task_instruction_evidence_locator_invalid",
            )
            task_started = [
                event for event in events.values() if event.get("event_type") == "task_started"
            ]
            _require(len(task_started) == 1, "task_started_event_not_unique")
            expected_projection = task_started[0]["payload"].get("task_goal")
            _require(
                isinstance(expected_projection, str)
                and _digest(expected_projection.encode("utf-8"))
                == frozen_record["task"]["task_instruction_sha256"],
                "task_instruction_projection_source_mismatch",
            )
            _require(
                artifact.get("source_event_type") is None
                and artifact.get("projection_path") == "task.instruction",
                "task_instruction_projection_contract_invalid",
            )
            _require(
                artifact.get("projection") == expected_projection,
                "curation_evidence_projection_mismatch",
            )
            _require(
                artifact.get("projection_sha256") == canonical_sha256(expected_projection),
                "curation_evidence_projection_hash_mismatch",
            )
            _validate_model_visible_projection(
                artifact,
                target_request=target_request,
                expected_projection=expected_projection,
            )
            continue
        _require(
            isinstance(event_id, str) and event_id in events, "curation_evidence_event_missing"
        )
        event = events[event_id]
        _require(
            event.get("seq") == event_seq and event_seq <= cutoff["event_seq"],
            "curation_evidence_after_request_cutoff",
        )
        derived_step = event.get("payload", {}).get("step_index")
        if derived_step is None:
            derived_step = step_by_id.get(event.get("payload", {}).get("step_id"))
        _require(derived_step == observed_step, "curation_evidence_step_mismatch")
        if role not in {"target_pre", "source_history"}:
            _require(observed_step < target_step, "transformation_evidence_not_prior_step")
        projection_path = reference["projection_path"]
        expected_event_type: str
        if role in {"target_pre", "source_pre"}:
            expected_event_type = "step_started"
            _require(
                projection_path == "payload.observation.screenshot.pixel_blob",
                "pre_state_projection_path_invalid",
            )
            expected_projection = _current_gui_ref(event["payload"].get("observation"))
            if role == "target_pre":
                _require(observed_step == target_step, "target_pre_step_mismatch")
                _require(
                    expected_projection["digest"]
                    == frozen_record["frozen_capsule"]["current_gui_blob"]["digest"],
                    "target_pre_gui_mismatch",
                )
        elif role == "source_history":
            expected_event_type = "model_request"
            _require(
                projection_path == "payload.request_view.messages",
                "source_history_projection_path_invalid",
            )
            _require(
                event_id == frozen_record["decision"]["request_event_id"],
                "target_request_history_event_mismatch",
            )
            expected_projection = target_request["payload"].get("request_view", {}).get("messages")
        else:
            expected_event_type = "transition_completed"
            execution_result = event["payload"].get("execution_result", {})
            if role == "tool_response":
                _require(
                    projection_path == "payload.execution_result.agent_visible_tool_result",
                    "tool_response_projection_path_invalid",
                )
                expected_projection = execution_result.get("agent_visible_tool_result")
            else:
                _require(role == "ask_user_response", "evidence_role_unhandled")
                _require(
                    projection_path == "payload.execution_result.ask_user_response",
                    "ask_user_projection_path_invalid",
                )
                expected_projection = execution_result.get("ask_user_response")
        _require(event.get("event_type") == expected_event_type, "evidence_event_type_invalid")
        _require(
            artifact.get("source_event_type") == expected_event_type
            and artifact.get("projection") == expected_projection,
            "curation_evidence_projection_mismatch",
        )
        _require(
            artifact.get("projection_sha256") == canonical_sha256(expected_projection),
            "curation_evidence_projection_hash_mismatch",
        )
        _validate_model_visible_projection(
            artifact,
            target_request=target_request,
            expected_projection=expected_projection,
        )
    _require(order_keys == sorted(order_keys), "curation_evidence_refs_not_ordered")


def _validate_model_visible_projection(
    artifact: Mapping[str, Any],
    *,
    target_request: Mapping[str, Any],
    expected_projection: Any,
) -> None:
    """Prove curator bytes are an exact projection of the target model request."""

    proof = artifact.get("visibility_proof")
    _require(isinstance(proof, Mapping), "curation_visibility_proof_missing")
    _require(
        proof.get("visibility_contract") == "model-visible-request-projection/v1"
        and proof.get("model_visible_at_or_before_request") is True
        and proof.get("target_request_event_id") == target_request.get("event_id")
        and proof.get("target_request_event_seq") == target_request.get("seq"),
        "curation_visibility_proof_request_mismatch",
    )
    request_view = target_request["payload"].get("request_view")
    _require(isinstance(request_view, Mapping), "curation_visibility_request_view_missing")
    messages = request_view.get("messages")
    _require(isinstance(messages, list), "curation_visibility_messages_missing")
    locator = proof.get("request_locator")
    _require(isinstance(locator, Mapping), "curation_visibility_locator_invalid")
    kind = locator.get("locator_kind")
    role = artifact.get("evidence_role")
    if kind == "FULL_MESSAGES":
        _require(role == "source_history", "full_messages_visibility_role_invalid")
        _require(expected_projection == messages, "full_messages_visibility_projection_mismatch")
        return

    message_index = _nonnegative_int(locator.get("message_index"), "visibility.message_index")
    _require(message_index < len(messages), "visibility_message_index_out_of_bounds")
    message = messages[message_index]
    _require(isinstance(message, Mapping), "visibility_message_invalid")
    content = message.get("content")
    if kind == "IMAGE_CONTENT_BLOB":
        _require(role in {"source_pre", "target_pre"}, "image_visibility_role_invalid")
        item_index = _nonnegative_int(
            locator.get("content_item_index"),
            "visibility.content_item_index",
        )
        _require(
            isinstance(content, list) and item_index < len(content), "visibility_image_item_missing"
        )
        try:
            projected = content[item_index]["image_url"]["url"]["$externalized_data_url"][
                "content_blob"
            ]
        except (KeyError, TypeError) as error:
            raise CausalReplayRegistryError(
                "visibility_image_projection_unresolved",
                "visibility image projection unresolved",
            ) from error
        _require(projected == expected_projection, "visibility_image_projection_mismatch")
        return

    _require(kind == "TEXT_SPAN", "visibility_locator_kind_invalid")
    _require(
        role in {"ask_user_response", "task_instruction", "tool_response"},
        "text_visibility_role_invalid",
    )
    item_index = locator.get("content_item_index")
    if locator.get("field_path") == "content":
        _require(item_index is None and isinstance(content, str), "visibility_scalar_text_invalid")
        text = content
    else:
        item_offset = _nonnegative_int(item_index, "visibility.content_item_index")
        _require(
            isinstance(content, list) and item_offset < len(content), "visibility_text_item_missing"
        )
        item = content[item_offset]
        _require(
            isinstance(item, Mapping) and isinstance(item.get("text"), str),
            "visibility_text_field_missing",
        )
        text = item["text"]
    selected = _validate_exact_span(text, locator, code="visibility_text_span")
    _require(
        isinstance(expected_projection, str) and selected == expected_projection,
        "visibility_text_projection_mismatch",
    )


def _validate_curation_input_manifest(
    root: Path,
    source_base: Path,
    reference: Mapping[str, Any],
    *,
    expected_channel: str,
    expected_unit: Mapping[str, Any],
    expected_evidence_refs: Any,
    frozen_record: Mapping[str, Any],
) -> None:
    manifest = _load_schema_bound_ref(
        root,
        reference,
        schema_name="curation_input_manifest.schema.json",
    )
    _require(manifest.get("channel") == expected_channel, "curation_input_channel_mismatch")
    _require(manifest.get("unit_ref") == expected_unit, "curation_input_unit_mismatch")
    cutoff = manifest.get("request_cutoff")
    expected_cutoff = frozen_record["decision"]["request_cutoff"]
    _require(
        cutoff
        == {
            "event_id": expected_cutoff["event_id"],
            "event_seq": expected_cutoff["event_seq"],
            "target_step": expected_cutoff["target_step"],
        },
        "curation_input_cutoff_mismatch",
    )
    evidence_refs = manifest.get("evidence_refs")
    _require(evidence_refs == expected_evidence_refs, "curation_input_evidence_set_mismatch")
    _require(
        manifest.get("evidence_set_sha256") == canonical_sha256(evidence_refs),
        "curation_input_evidence_set_hash_mismatch",
    )
    _validate_curation_evidence_role_completeness(expected_channel, evidence_refs)
    allowed = (
        frozenset({"ask_user_response", "target_pre", "task_instruction", "tool_response"})
        if expected_channel == "ACTION_GOLD"
        else frozenset(
            {
                "source_history",
                "source_pre",
                "target_pre",
                "task_instruction",
                "tool_response",
                "ask_user_response",
            }
        )
    )
    _validate_evidence_refs(
        root,
        source_base,
        evidence_refs,
        frozen_record=frozen_record,
        allowed_roles=allowed,
    )


def _validate_curation_evidence_role_completeness(channel: str, evidence_refs: Any) -> None:
    """Require the minimum model-visible evidence needed by each curator channel."""

    _require(isinstance(evidence_refs, list), "curation_evidence_refs_invalid")
    roles = [
        reference.get("evidence_role")
        for reference in evidence_refs
        if isinstance(reference, Mapping)
    ]
    required = (
        frozenset({"task_instruction", "target_pre"})
        if channel == "ACTION_GOLD"
        else frozenset({"source_history"})
    )
    _require(
        channel in {"ACTION_GOLD", "TRANSFORMATION"},
        "curation_input_channel_invalid",
    )
    missing = sorted(required.difference(roles))
    _require(
        not missing,
        "curation_input_required_evidence_roles_missing",
        channel=channel,
        missing=missing,
    )


def _validate_action_predicate_semantics(
    predicate: Mapping[str, Any],
    *,
    normalized_action: Mapping[str, Any] | None = None,
) -> None:
    kind = predicate.get("predicate_kind")
    if kind == "EXACT_NORMALIZED_ACTION":
        _require(
            isinstance(normalized_action, Mapping),
            "exact_action_predicate_payload_missing",
        )
        _validate_original_action(
            normalized_action,
            {"parse_outcome": "returned", "parse_exception": None},
        )
        _require(
            predicate.get("action_type") == normalized_action["value"]["action_type"],
            "exact_action_predicate_type_mismatch",
        )
    elif kind == "TEXT_VARIANTS":
        action_type = predicate.get("action_type")
        expected_field = _TEXT_PREDICATE_FIELD_BY_ACTION_TYPE.get(action_type)
        _require(expected_field is not None, "text_predicate_action_type_invalid")
        _require(
            predicate.get("field") == expected_field,
            "text_predicate_field_mismatch",
            action_type=action_type,
            expected_field=expected_field,
        )


def _validate_gold_bundle(
    root: Path,
    source_base: Path,
    bundle: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
) -> None:
    _require(bundle.get("unit_ref") == expected_unit, "gold_unit_ref_mismatch")
    _validate_curation_input_manifest(
        root,
        source_base,
        bundle["curation_input_manifest_ref"],
        expected_channel="ACTION_GOLD",
        expected_unit=expected_unit,
        expected_evidence_refs=bundle.get("evidence_refs"),
        frozen_record=frozen_record,
    )
    production = bundle["production_contract"]
    _require(
        production.get("model_config_manifest_sha256") == MODEL_CONFIG_MANIFEST_SHA256,
        "gold_model_config_manifest_mismatch",
    )
    model_manifest = _load_json_object(
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff"
        / "g1"
        / "model_config_manifest.v1.json"
    )
    model = next(
        entry
        for entry in model_manifest["models"]
        if entry["model_id"] == frozen_record["model_id"]
    )
    _require(
        production.get("production_parser_sha256") == model["parser_implementation"]["sha256"]
        and production.get("normalized_action_schema_sha256")
        == model["normalized_action_schema"]["sha256"],
        "gold_parser_or_action_schema_mismatch",
    )
    predicates = bundle["accepted_next_action_set"]["predicates"]
    predicate_ids = [predicate.get("predicate_id") for predicate in predicates]
    _require(
        predicate_ids == sorted(predicate_ids) and len(predicate_ids) == len(set(predicate_ids)),
        "accepted_action_predicates_not_canonical",
    )
    current_digest = frozen_record["frozen_capsule"]["current_gui_blob"]["digest"]
    images = frozen_record["frozen_capsule"]["request_images"]
    current_image = next(
        (image for image in images if image["content_blob"]["digest"] == current_digest), None
    )
    _require(current_image is not None, "gold_current_gui_missing")
    width = current_image.get("width")
    height = current_image.get("height")
    for predicate in predicates:
        kind = predicate["predicate_kind"]
        if kind == "EXACT_NORMALIZED_ACTION":
            normalized = _load_schema_bound_ref(
                root,
                predicate["normalized_action_ref"],
                schema_name="normalized_action.schema.json",
            )
            action = normalized.get("normalized_action")
            _require(isinstance(action, Mapping), "normalized_action_payload_invalid")
            _require(
                normalized.get("normalized_action_sha256") == canonical_sha256(action),
                "normalized_action_payload_hash_mismatch",
            )
            _validate_action_predicate_semantics(
                predicate,
                normalized_action=action,
            )
        else:
            _validate_action_predicate_semantics(predicate)
        if kind in {"POINT_REGION", "DRAG_REGION"}:
            _require(
                isinstance(width, int) and isinstance(height, int),
                "gold_gui_dimensions_missing",
            )
            region_fields = (
                ("regions",) if kind == "POINT_REGION" else ("start_regions", "end_regions")
            )
            for field in region_fields:
                for region in predicate[field]:
                    _validate_region_bounds(region, width=width, height=height)


def _validate_region_bounds(region: Mapping[str, Any], *, width: int, height: int) -> None:
    if region.get("shape") == "BOUNDING_BOX":
        _require(
            0 <= region["x_min"] <= region["x_max"] < width
            and 0 <= region["y_min"] <= region["y_max"] < height,
            "gold_region_out_of_bounds",
        )
        return
    vertices = region.get("vertices")
    _require(
        isinstance(vertices, list)
        and all(0 <= point[0] < width and 0 <= point[1] < height for point in vertices),
        "gold_polygon_out_of_bounds",
    )


def _validate_review_ledger(
    root: Path,
    source_base: Path,
    ledger: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    expected_channel: str,
    expected_payload_ref: Mapping[str, Any] | None,
    expected_input_ref: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
    expected_disposition: str = "ACCEPT",
    expected_exclusion_reason: str | None = None,
) -> list[str]:
    _require(ledger.get("channel") == expected_channel, "review_ledger_channel_mismatch")
    subject = ledger["subject"]
    for key in (
        "unit_kind",
        "unit_id",
        "unit_record_sha256",
        "frozen_capsule_sha256",
        "request_cutoff_event_id",
        "request_cutoff_event_seq",
    ):
        _require(
            subject.get(key) == expected_unit.get(key), "review_ledger_subject_mismatch", field=key
        )
    input_ref = ledger["curation_input_manifest_ref"]
    _require(input_ref == expected_input_ref, "review_bundle_input_manifest_ref_mismatch")
    input_manifest = _load_schema_bound_ref(
        root,
        input_ref,
        schema_name="curation_input_manifest.schema.json",
    )
    _validate_curation_input_manifest(
        root,
        source_base,
        input_ref,
        expected_channel=expected_channel,
        expected_unit=expected_unit,
        expected_evidence_refs=input_manifest["evidence_refs"],
        frozen_record=frozen_record,
    )
    reviews = ledger["independent_reviews"]
    identities = [review["reviewer_identity_sha256"] for review in reviews]
    review_ids = [review["review_id"] for review in reviews]
    artifact_hashes = [review["review_artifact_ref"]["sha256"] for review in reviews]
    _require(
        len(set(identities)) == len(set(review_ids)) == len(set(artifact_hashes)) == 2,
        "independent_review_identity_or_artifact_collision",
    )
    for review in reviews:
        _require(
            review["curation_input_manifest_sha256"] == input_ref["sha256"],
            "review_input_manifest_mismatch",
        )
        _load_canonical_ref_object(root, review["review_artifact_ref"])
        proposal_ref = review["proposal"].get("payload_ref")
        if proposal_ref is not None:
            _load_canonical_ref_object(root, proposal_ref)
    resolution = ledger["resolution"]
    if ledger["material_disagreement"] is False:
        _require(reviews[0]["proposal"] == reviews[1]["proposal"], "review_agreement_not_exact")
        proposal = reviews[0]["proposal"]
    else:
        adjudication = ledger["adjudication"]
        identity = adjudication["reviewer_identity_sha256"]
        _require(identity not in identities, "adjudicator_identity_not_independent")
        _require(
            set(adjudication["compared_review_artifact_sha256s"]) == set(artifact_hashes),
            "adjudication_compared_hashes_mismatch",
        )
        _load_canonical_ref_object(root, adjudication["adjudication_artifact_ref"])
        proposal = adjudication["proposal"]
        identities.append(identity)
    expected_resolution = {
        "resolution_kind": "ADJUDICATED" if ledger["material_disagreement"] else "AGREEMENT",
        "disposition": proposal["disposition"],
        "resolved_payload_ref": proposal["payload_ref"],
        "exclusion_reason": proposal["exclusion_reason"],
    }
    _require(resolution == expected_resolution, "review_resolution_mismatch")
    _require(
        resolution["disposition"] == expected_disposition,
        "review_resolution_disposition_mismatch",
    )
    if expected_disposition == "ACCEPT":
        _require(
            expected_payload_ref is not None
            and resolution["resolved_payload_ref"]["sha256"] == expected_payload_ref["sha256"]
            and resolution["exclusion_reason"] is None,
            "review_resolved_payload_mismatch",
        )
    else:
        _require(
            expected_disposition == "EXCLUDE"
            and expected_payload_ref is None
            and resolution["resolved_payload_ref"] is None
            and resolution["exclusion_reason"] == expected_exclusion_reason,
            "review_exclusion_reason_mismatch",
        )
    return identities


def _request_view_for_frozen_unit(
    source_base: Path, frozen_record: Mapping[str, Any]
) -> Mapping[str, Any]:
    events = _raw_events_for_frozen_unit(source_base, frozen_record)
    event_id = frozen_record["decision"]["request_event_id"]
    event = _event(events, event_id, "model_request")
    request_view = event["payload"].get("request_view")
    _require(isinstance(request_view, Mapping), "admission_request_view_missing")
    _require(
        _digest(canonical_json_bytes(request_view, newline=False))
        == frozen_record["frozen_capsule"]["request_view_sha256"],
        "admission_request_view_hash_mismatch",
    )
    return request_view


def _request_record_text(request_view: Mapping[str, Any], request_path: str) -> str:
    messages = request_view.get("messages")
    _require(isinstance(messages, list), "admission_request_messages_missing")
    qwen_match = _QWEN_RECORD_PATH_RE.fullmatch(request_path)
    raw_match = _RAW_PATH_RE.fullmatch(request_path)
    try:
        if qwen_match is not None:
            value = messages[int(qwen_match.group("message"))]["content"][
                int(qwen_match.group("block"))
            ]["text"]
        else:
            _require(raw_match is not None, "transformation_request_path_invalid")
            value = messages[int(raw_match.group("message"))]["content"]
    except (IndexError, KeyError, TypeError) as error:
        raise CausalReplayRegistryError(
            "transformation_request_path_unresolved", request_path
        ) from error
    _require(isinstance(value, str), "transformation_request_record_not_text")
    return value


def _semantic_history_records(
    request_view: Mapping[str, Any],
) -> list[tuple[str, int, int, str]]:
    """Enumerate complete model-visible semantic history records in request order."""

    messages = request_view.get("messages")
    _require(isinstance(messages, list), "semantic_history_messages_missing")
    records: list[tuple[str, int, int, str]] = []
    for message_index, message in enumerate(messages):
        _require(isinstance(message, Mapping), "semantic_history_message_invalid")
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str) and content:
            records.append(
                (
                    f"payload.request_view.messages[{message_index}].content",
                    0,
                    len(content),
                    _digest(content.encode("utf-8")),
                )
            )
            continue
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, Mapping):
                continue
            text = block.get("text")
            if not isinstance(text, str) or "Task progress" not in text:
                continue
            request_path = (
                f"payload.request_view.messages[{message_index}].content[{block_index}].text"
            )
            for match in _QWEN_SEMANTIC_RECORD_RE.finditer(text):
                selected = match.group(0)
                records.append(
                    (
                        request_path,
                        match.start(),
                        match.end(),
                        _digest(selected.encode("utf-8")),
                    )
                )
    return records


def _expected_history_depth(request_view: Mapping[str, Any], target: Mapping[str, Any]) -> int:
    binding = target["record_binding"]
    semantic = binding["semantic_record"]
    identity = (
        binding["request_path"],
        semantic["char_start"],
        semantic["char_end"],
        semantic["span_sha256"],
    )
    records = _semantic_history_records(request_view)
    matches = [index for index, record in enumerate(records) if record == identity]
    _require(len(matches) == 1, "semantic_history_record_not_unique")
    return len(records) - matches[0]


def _validate_exact_span(text: str, span: Mapping[str, Any], *, code: str) -> str:
    start = span.get("char_start")
    end = span.get("char_end")
    byte_start = span.get("utf8_byte_start")
    byte_end = span.get("utf8_byte_end")
    _require(
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(text),
        f"{code}_char_bounds_invalid",
    )
    encoded = text.encode("utf-8")
    _require(
        isinstance(byte_start, int)
        and not isinstance(byte_start, bool)
        and isinstance(byte_end, int)
        and not isinstance(byte_end, bool)
        and 0 <= byte_start < byte_end <= len(encoded),
        f"{code}_utf8_bounds_invalid",
    )
    selected = text[start:end]
    selected_bytes = selected.encode("utf-8")
    _require(
        len(text[:start].encode("utf-8")) == byte_start
        and len(text[:end].encode("utf-8")) == byte_end
        and encoded[byte_start:byte_end] == selected_bytes,
        f"{code}_char_utf8_offset_mismatch",
    )
    _require(_digest(selected_bytes) == span.get("span_sha256"), f"{code}_hash_mismatch")
    return selected


def _validate_target_locator(
    request_view: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    request_event_id: str,
) -> tuple[tuple[Any, ...], str, str]:
    binding = target["record_binding"]
    request_path = binding["request_path"]
    text = _request_record_text(request_view, request_path)
    messages = request_view.get("messages")
    _require(isinstance(messages, list), "transformation_request_messages_missing")
    qwen_match = _QWEN_RECORD_PATH_RE.fullmatch(request_path)
    raw_match = _RAW_PATH_RE.fullmatch(request_path)
    _require(qwen_match is not None or raw_match is not None, "transformation_request_path_invalid")
    message_index = int((qwen_match or raw_match).group("message"))
    message = messages[message_index]
    _require(isinstance(message, Mapping), "transformation_request_message_invalid")
    expected_content_index = int(qwen_match.group("block")) if qwen_match is not None else None
    expected_content_kind = "TEXT" if qwen_match is not None else "SCALAR_TEXT"
    expected_record_class = (
        "QWEN_FLAT_PROGRESS_STEP" if qwen_match is not None else "MAI_RAW_ASSISTANT_MESSAGE"
    )
    _require(
        binding["message_index"] == message_index
        and binding["content_item_index"] == expected_content_index
        and binding["message_role"] == message.get("role")
        and binding["content_item_kind"] == expected_content_kind
        and binding["representation_record_class"] == expected_record_class,
        "transformation_record_structure_mismatch",
    )
    _require(
        binding["record_sha256"] == _digest(text.encode("utf-8"))
        and binding["record_codepoint_count"] == len(text)
        and binding["record_utf8_byte_count"] == len(text.encode("utf-8")),
        "transformation_record_binding_mismatch",
    )
    _require(
        binding["record_identity_sha256"]
        == _stable_hash(
            "request-record",
            request_event_id,
            request_path,
            binding["record_sha256"],
        ),
        "transformation_record_identity_mismatch",
    )
    semantic_text = _validate_exact_span(text, binding["semantic_record"], code="semantic_record")
    edit_text = _validate_exact_span(text, target["edit_span"], code="edit_span")
    semantic = binding["semantic_record"]
    edit = target["edit_span"]
    _require(
        semantic["char_start"] <= edit["char_start"] < edit["char_end"] <= semantic["char_end"],
        "edit_span_outside_semantic_record",
    )
    _require(
        binding["history_depth"] == _expected_history_depth(request_view, target),
        "transformation_history_depth_mismatch",
    )
    order_key = (
        binding["message_index"],
        -1 if binding["content_item_index"] is None else binding["content_item_index"],
        edit["char_start"],
        edit["span_sha256"],
    )
    return order_key, edit_text, semantic_text


def _location_bucket(target: Mapping[str, Any]) -> dict[str, str]:
    binding = target["record_binding"]
    edit = target["edit_span"]
    record_length = binding["record_codepoint_count"]
    position = min(2, (edit["char_start"] * 3) // record_length)
    relative_third = ("LEADING", "MIDDLE", "TRAILING")[position]
    return {
        "message_role": binding["message_role"],
        "content_item_kind": binding["content_item_kind"],
        "representation_record_class": binding["representation_record_class"],
        "relative_third": relative_third,
    }


def _span_identity(target: Mapping[str, Any]) -> tuple[Any, ...]:
    binding = target["record_binding"]
    edit = target["edit_span"]
    return (
        binding["record_identity_sha256"],
        binding["request_path"],
        edit["char_start"],
        edit["char_end"],
        edit["utf8_byte_start"],
        edit["utf8_byte_end"],
        edit["span_sha256"],
    )


def _validate_focal_candidate_assignments(
    focal_by_id: Mapping[str, tuple[Mapping[str, Any], str]],
    binding_by_candidate: Mapping[str, Mapping[str, Any]],
) -> None:
    candidate_coverage: Counter[str] = Counter()
    for target, _ in focal_by_id.values():
        candidate_ids = target.get("source_candidate_ids")
        _require(
            isinstance(candidate_ids, list) and bool(candidate_ids),
            "focal_target_source_candidates_empty",
        )
        _require(
            candidate_ids == sorted(set(candidate_ids)),
            "target_source_candidates_not_ordered_or_unique",
        )
        for candidate_id in candidate_ids:
            _require(
                candidate_id in binding_by_candidate,
                "target_source_candidate_not_frozen",
            )
            candidate_coverage[candidate_id] += 1
    _require(
        set(candidate_coverage) == set(binding_by_candidate),
        "focal_candidate_coverage_incomplete",
    )
    _require(
        all(count == 1 for count in candidate_coverage.values()),
        "focal_candidate_coverage_not_exactly_once",
    )


def _validate_target_set(
    request_view: Mapping[str, Any],
    targets: Any,
    *,
    label: str,
    request_event_id: str,
) -> tuple[list[tuple[Any, ...]], dict[str, tuple[Mapping[str, Any], str]]]:
    _require(isinstance(targets, list), f"{label}_target_set_invalid")
    keys: list[tuple[Any, ...]] = []
    by_id: dict[str, tuple[Mapping[str, Any], str]] = {}
    intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ordinal, target in enumerate(targets):
        _require(target.get("ordinal") == ordinal, f"{label}_target_ordinal_invalid")
        key, edit_text, _ = _validate_target_locator(
            request_view,
            target,
            request_event_id=request_event_id,
        )
        keys.append(key)
        target_id = target["target_id"]
        _require(target_id not in by_id, f"{label}_target_id_duplicate")
        by_id[target_id] = (target, edit_text)
        binding = target["record_binding"]
        edit = target["edit_span"]
        intervals[binding["record_identity_sha256"]].append((edit["char_start"], edit["char_end"]))
    _require(keys == sorted(keys), f"{label}_target_set_not_ordered")
    _require(len(keys) == len(set(keys)), f"{label}_target_set_duplicate")
    for values in intervals.values():
        ordered = sorted(values)
        _require(
            all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
            f"{label}_target_set_overlap",
        )
    return keys, by_id


def _validate_transformation_bundle(
    root: Path,
    source_base: Path,
    plan: Mapping[str, Any],
    *,
    frozen_record: Mapping[str, Any],
    expected_unit: Mapping[str, Any],
) -> None:
    _require(plan.get("unit_ref") == expected_unit, "transformation_unit_ref_mismatch")
    _require(
        plan.get("invariants") == _expected_transformation_invariants(expected_unit["unit_kind"]),
        "transformation_invariants_mismatch",
    )
    _validate_curation_input_manifest(
        root,
        source_base,
        plan["curation_input_manifest_ref"],
        expected_channel="TRANSFORMATION",
        expected_unit=expected_unit,
        expected_evidence_refs=plan.get("evidence_refs"),
        frozen_record=frozen_record,
    )
    _require(
        plan["focal_target_set_sha256"] == canonical_sha256(plan["focal_target_set"])
        and plan["oracle_target_set_sha256"] == canonical_sha256(plan["oracle_target_set"]),
        "transformation_target_set_hash_mismatch",
    )
    request_view = _request_view_for_frozen_unit(source_base, frozen_record)
    request_event_id = frozen_record["decision"]["request_event_id"]
    _, focal_by_id = _validate_target_set(
        request_view,
        plan["focal_target_set"],
        label="focal",
        request_event_id=request_event_id,
    )
    _, oracle_by_id = _validate_target_set(
        request_view,
        plan["oracle_target_set"],
        label="oracle",
        request_event_id=request_event_id,
    )
    focal_identities = {_span_identity(target) for target, _ in focal_by_id.values()}
    oracle_identities = {_span_identity(target) for target, _ in oracle_by_id.values()}
    if expected_unit["unit_kind"] == "STRICT_MHR":
        _require(focal_identities <= oracle_identities, "oracle_target_set_not_superset")
    else:
        _require(not oracle_identities, "clean_control_oracle_target_forbidden")

    frozen_bindings = frozen_record["transformation_refs"]["audited_exposure_bindings"]
    binding_by_candidate = {binding["candidate_id"]: binding for binding in frozen_bindings}
    _validate_focal_candidate_assignments(focal_by_id, binding_by_candidate)
    for target, _ in focal_by_id.values():
        candidate_ids = target["source_candidate_ids"]
        for candidate_id in candidate_ids:
            frozen = binding_by_candidate[candidate_id]
            binding = target["record_binding"]
            _require(
                binding["record_identity_sha256"] == frozen["record_identity_sha256"]
                and binding["request_path"] == frozen["request_path"]
                and binding["record_sha256"] == frozen["container_sha256"],
                "target_record_not_bound_to_g1_1_exposure",
            )
            edit = target["edit_span"]
            if frozen["edit_span_status"] == "G1_1_FROZEN":
                _require(
                    edit
                    == {
                        "span_origin": "FROZEN_G1_1_EXACT_EXPOSURE",
                        **frozen["focal_edit_spans"][0],
                    },
                    "qwen_edit_span_not_exact_g1_1_exposure",
                )
            else:
                _require(
                    edit["span_origin"] == "INDEPENDENT_G1_6_CURATION",
                    "raw_edit_span_not_independently_curated",
                )
                envelope = frozen.get("curation_envelope")
                if envelope is not None:
                    _require(
                        envelope["char_start"]
                        <= edit["char_start"]
                        < edit["char_end"]
                        <= envelope["char_end"],
                        "raw_edit_span_outside_curation_envelope",
                    )

    protected: list[tuple[str, str, int, int, int, int, str, str]] = []
    for protected_span in plan["protected_spans"]:
        text = _request_record_text(request_view, protected_span["request_path"])
        _require(
            protected_span["record_identity_sha256"]
            == _stable_hash(
                "request-record",
                request_event_id,
                protected_span["request_path"],
                _digest(text.encode("utf-8")),
            ),
            "protected_span_record_identity_mismatch",
        )
        selected = _validate_exact_span(text, protected_span["span"], code="protected_span")
        protected.append(
            (
                protected_span["record_identity_sha256"],
                protected_span["request_path"],
                protected_span["span"]["char_start"],
                protected_span["span"]["char_end"],
                protected_span["span"]["utf8_byte_start"],
                protected_span["span"]["utf8_byte_end"],
                protected_span["span"]["span_sha256"],
                protected_span["segment_kind"],
            )
        )
        if protected_span["segment_kind"] == "TOOL_CALL":
            _require(
                "<tool_call>" in selected and "</tool_call>" in selected,
                "tool_call_protection_incomplete",
            )
    for target, _ in [*focal_by_id.values(), *oracle_by_id.values()]:
        binding = target["record_binding"]
        edit = target["edit_span"]
        for record_identity, _, start, end, _, _, _, _ in protected:
            if record_identity == binding["record_identity_sha256"]:
                _require(
                    edit["char_end"] <= start or end <= edit["char_start"],
                    "target_intersects_protected_span",
                )

    correction_ids = [correction["target_id"] for correction in plan["corrections"]]
    if expected_unit["unit_kind"] == "STRICT_MHR":
        _require(
            sorted(correction_ids) == sorted(focal_by_id),
            "correction_target_coverage_invalid",
        )
    else:
        _require(not correction_ids, "clean_control_correction_forbidden")
    tokenizer = _load_bound_tokenizer(plan["tokenizer_binding"], frozen_record["model_id"])
    transformation_evidence_ids = {reference["ref_id"] for reference in plan["evidence_refs"]}
    correction_text_by_target_id: dict[str, str] = {}
    for correction in plan["corrections"]:
        _require(
            correction["evidence_ref_ids"] == sorted(correction["evidence_ref_ids"])
            and set(correction["evidence_ref_ids"]) <= transformation_evidence_ids,
            "correction_evidence_refs_invalid",
        )
        text_payload = _load_schema_bound_ref(
            root,
            correction["correction_utf8_ref"],
            schema_name="utf8_text.schema.json",
        )
        correction_text = text_payload.get("text")
        _require(
            isinstance(correction_text, str) and bool(correction_text.strip()),
            "correction_text_invalid",
        )
        correction_text_by_target_id[correction["target_id"]] = correction_text
        encoded = correction_text.encode("utf-8")
        _require(
            correction["utf8_byte_count"] == len(encoded)
            and correction["codepoint_count"] == len(correction_text)
            and text_payload["text_utf8_sha256"] == _digest(encoded)
            and text_payload["utf8_byte_count"] == len(encoded)
            and text_payload["codepoint_count"] == len(correction_text)
            and correction["token_count"]
            == len(tokenizer.encode(correction_text, add_special_tokens=False)),
            "correction_length_or_token_count_mismatch",
        )

    sham = plan["sham_benign_edit"]
    _, benign_text, _ = _validate_target_locator(
        request_view,
        sham["benign_target"],
        request_event_id=request_event_id,
    )
    matched_id = sham["matched_focal_target_id"]
    _require(matched_id in focal_by_id, "sham_matched_focal_target_missing")
    _require(
        matched_id == plan["focal_target_set"][0]["target_id"],
        "sham_must_match_first_focal_target",
    )
    focal_target, _ = focal_by_id[matched_id]
    focal_count = sum(
        len(tokenizer.encode(text, add_special_tokens=False)) for _, text in focal_by_id.values()
    )
    benign_count = len(tokenizer.encode(benign_text, add_special_tokens=False))
    _require(
        sham["focal_token_count"] == focal_count
        and sham["benign_token_count"] == benign_count
        and sham["absolute_token_difference"] == abs(focal_count - benign_count),
        "sham_token_count_mismatch",
    )
    _require(
        (5 * benign_count >= 4 * focal_count and 4 * benign_count <= 5 * focal_count)
        or abs(focal_count - benign_count) <= 4,
        "sham_token_match_invalid",
    )
    location = sham["location_match"]
    focal_bucket = _location_bucket(focal_target)
    benign_bucket = _location_bucket(sham["benign_target"])
    _require(
        location["focal_bucket"] == focal_bucket
        and location["benign_bucket"] == benign_bucket
        and focal_bucket == benign_bucket,
        "sham_location_bucket_mismatch",
    )
    focal_depth = focal_target["record_binding"]["history_depth"]
    benign_depth = sham["benign_target"]["record_binding"]["history_depth"]
    same_record = (
        focal_target["record_binding"]["record_identity_sha256"]
        == sham["benign_target"]["record_binding"]["record_identity_sha256"]
    )
    _require(
        location["focal_history_depth"] == focal_depth
        and location["benign_history_depth"] == benign_depth
        and location["same_request_record"] is same_record
        and location["history_depth_difference"] == abs(focal_depth - benign_depth),
        "sham_history_depth_difference_mismatch",
    )
    benign_identity = _span_identity(sham["benign_target"])
    _require(
        benign_identity not in focal_identities | oracle_identities,
        "sham_span_is_misleading_target",
    )
    benign_binding = sham["benign_target"]["record_binding"]
    benign_edit = sham["benign_target"]["edit_span"]
    for target, _ in [*focal_by_id.values(), *oracle_by_id.values()]:
        target_binding = target["record_binding"]
        target_edit = target["edit_span"]
        if target_binding["record_identity_sha256"] == benign_binding["record_identity_sha256"]:
            _require(
                benign_edit["char_end"] <= target_edit["char_start"]
                or target_edit["char_end"] <= benign_edit["char_start"],
                "sham_span_overlaps_misleading_target",
            )
    for record_identity, _, start, end, _, _, _, _ in protected:
        if record_identity == benign_binding["record_identity_sha256"]:
            _require(
                benign_edit["char_end"] <= start or end <= benign_edit["char_start"],
                "sham_span_overlaps_protected_span",
            )

    _validate_delimiter_repairs(
        request_view,
        plan["delimiter_repairs"],
        focal_targets=[target for target, _ in focal_by_id.values()],
        oracle_targets=[target for target, _ in oracle_by_id.values()],
        benign_target=sham["benign_target"],
        protected_spans=protected,
        correction_text_by_target_id=correction_text_by_target_id,
    )

    if frozen_record["history_family"] == "RAW_REPLAY":
        target_records = {
            (
                target["record_binding"]["record_identity_sha256"],
                target["record_binding"]["request_path"],
            )
            for target, _ in [
                *focal_by_id.values(),
                *oracle_by_id.values(),
                (sham["benign_target"], benign_text),
            ]
            if target["record_binding"]["representation_record_class"]
            == "MAI_RAW_ASSISTANT_MESSAGE"
        }
        expected_tool_calls: set[tuple[str, str, int, int, int, int, str, str]] = set()
        for record_identity, request_path in target_records:
            text = _request_record_text(request_view, request_path)
            _require(
                text.count("<tool_call>") == text.count("</tool_call>"),
                "raw_tool_call_markup_unbalanced",
            )
            for match in re.finditer(r"<tool_call>.*?</tool_call>", text, flags=re.DOTALL):
                selected = match.group(0)
                expected_tool_calls.add(
                    (
                        record_identity,
                        request_path,
                        match.start(),
                        match.end(),
                        len(text[: match.start()].encode("utf-8")),
                        len(text[: match.end()].encode("utf-8")),
                        _digest(selected.encode("utf-8")),
                        "TOOL_CALL",
                    )
                )
        actual_tool_calls = {span for span in protected if span[-1] == "TOOL_CALL"}
        _require(
            actual_tool_calls == expected_tool_calls,
            "raw_tool_call_protected_set_mismatch",
        )


def _validate_delimiter_repairs(
    request_view: Mapping[str, Any],
    repairs_by_arm: Mapping[str, Any],
    *,
    focal_targets: Sequence[Mapping[str, Any]],
    oracle_targets: Sequence[Mapping[str, Any]],
    benign_target: Mapping[str, Any],
    protected_spans: Sequence[tuple[str, str, int, int, int, int, str, str]],
    correction_text_by_target_id: Mapping[str, str],
) -> None:
    selected_by_arm = {
        "MASK": list(focal_targets),
        "MASK_CORRECTION": list(focal_targets),
        "ORACLE_CLEAN": list(oracle_targets),
        "SHAM_BENIGN_EDIT": [benign_target],
    }
    _require(
        set(repairs_by_arm) == set(selected_by_arm),
        "delimiter_repair_arm_set_mismatch",
    )
    for arm_id, selected_targets in selected_by_arm.items():
        records: dict[str, tuple[str, str]] = {}
        edited_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
        replacement_intervals: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for target in selected_targets:
            binding = target["record_binding"]
            record_identity = binding["record_identity_sha256"]
            request_path = binding["request_path"]
            record = (request_path, _request_record_text(request_view, request_path))
            previous = records.setdefault(record_identity, record)
            _require(previous == record, "delimiter_repair_record_identity_collision")
            edit = target["edit_span"]
            edited_intervals[record_identity].append((edit["char_start"], edit["char_end"]))
            if (
                arm_id == "MASK_CORRECTION"
                and target.get("target_id") in correction_text_by_target_id
            ):
                replacement = correction_text_by_target_id[target["target_id"]]
                _require(
                    isinstance(replacement, str) and bool(replacement.strip()),
                    "delimiter_repair_correction_text_invalid",
                )
                replacement_intervals[record_identity].append(
                    (edit["char_start"], edit["char_end"], replacement)
                )

        repairs = repairs_by_arm[arm_id]
        repair_ids: set[str] = set()
        repair_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
        declared_repair_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for repair in repairs:
            declared_span = repair["deleted_syntax_span"]
            declared_repair_intervals[repair["record_identity_sha256"]].append(
                (declared_span["char_start"], declared_span["char_end"])
            )
        for repair in repairs:
            repair_id = repair["repair_id"]
            _require(repair_id not in repair_ids, "delimiter_repair_id_duplicate")
            repair_ids.add(repair_id)
            record_identity = repair["record_identity_sha256"]
            record = records.get(record_identity)
            _require(record is not None, "delimiter_repair_changes_unselected_record")
            _, text = record
            selected = _validate_exact_span(
                text,
                repair["deleted_syntax_span"],
                code="delimiter_repair_span",
            )
            patterns = _DELIMITER_REPAIR_PATTERNS.get(repair["operation"], ())
            _require(
                any(pattern.fullmatch(selected) is not None for pattern in patterns),
                "delimiter_repair_syntax_not_whitelisted",
            )
            span = repair["deleted_syntax_span"]
            start, end = span["char_start"], span["char_end"]
            _require(
                all(
                    end <= left or right <= start
                    for left, right in edited_intervals[record_identity]
                ),
                "delimiter_repair_overlaps_semantic_edit",
            )
            _require(
                any(
                    (
                        end <= target_start
                        and (not text[end:target_start] or text[end:target_start].isspace())
                    )
                    or (
                        target_end <= start
                        and (not text[target_end:start] or text[target_end:start].isspace())
                    )
                    for target_start, target_end in edited_intervals[record_identity]
                ),
                "delimiter_repair_not_adjacent_to_selected_target",
            )
            _require(
                _delimiter_repair_is_causally_empty(
                    text,
                    selected,
                    start=start,
                    end=end,
                    target_intervals=edited_intervals[record_identity],
                    repair_intervals=declared_repair_intervals[record_identity],
                    replacement_intervals=replacement_intervals[record_identity],
                ),
                "delimiter_repair_not_causally_empty",
            )
            for protected_identity, _, left, right, _, _, _, _ in protected_spans:
                if protected_identity == record_identity:
                    _require(
                        end <= left or right <= start,
                        "delimiter_repair_overlaps_protected_span",
                    )
            repair_intervals[record_identity].append((start, end))
        for intervals in repair_intervals.values():
            ordered = sorted(intervals)
            _require(
                all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
                "delimiter_repair_spans_overlap",
            )


def _delimiter_repair_is_causally_empty(
    text: str,
    selected_syntax: str,
    *,
    start: int,
    end: int,
    target_intervals: Sequence[tuple[int, int]],
    repair_intervals: Sequence[tuple[int, int]],
    replacement_intervals: Sequence[tuple[int, int, str]],
) -> bool:
    stripped = selected_syntax.strip()
    all_intervals = [*target_intervals, *repair_intervals]

    if re.fullmatch(r"(?:Step\s+[1-9]\d*|Thought)\s*:", stripped) is not None:
        if stripped.startswith("Step"):
            line_start = text.rfind("\n", 0, start) + 1
            previous_separator = text.rfind(";", line_start, start)
            scope_start = max(line_start, previous_separator + 1)
            next_separator = text.find(";", end)
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            scope_end = next_separator + 1 if 0 <= next_separator < line_end else line_end
        else:
            scope_start = text.rfind("\n", 0, start) + 1
            scope_end = text.find("\n", end)
            if scope_end < 0:
                scope_end = len(text)
    elif stripped in {"<thinking>", "</thinking>"}:
        if stripped == "<thinking>":
            opening_start = start + selected_syntax.index("<thinking>")
            opening_end = opening_start + len("<thinking>")
            closing_start = text.find("</thinking>", opening_end)
        else:
            closing_start = start + selected_syntax.index("</thinking>")
            opening_start = text.rfind("<thinking>", 0, closing_start)
            opening_end = opening_start + len("<thinking>")
        if opening_start < 0 or closing_start < 0:
            return False
        closing_end = closing_start + len("</thinking>")
        if not (
            any(left <= opening_start and opening_end <= right for left, right in repair_intervals)
            and any(
                left <= closing_start and closing_end <= right for left, right in repair_intervals
            )
        ):
            return False
        scope_start, scope_end = opening_end, closing_start
    elif stripped == ";":
        semicolon = start + selected_syntax.index(";")
        line_start = text.rfind("\n", 0, semicolon) + 1
        previous_separator = text.rfind(";", line_start, semicolon)
        scope_start = max(line_start, previous_separator + 1)
        scope_end = semicolon + 1
        if not any(
            scope_start <= target_start < target_end <= semicolon
            for target_start, target_end in target_intervals
        ):
            return False
    else:
        return False

    if not any(
        scope_start <= target_start < target_end <= scope_end
        for target_start, target_end in target_intervals
    ):
        return False
    cursor = scope_start
    remaining: list[str] = []
    for left, right in sorted(all_intervals):
        clipped_left = max(scope_start, left)
        clipped_right = min(scope_end, right)
        if clipped_left >= clipped_right or clipped_right <= cursor:
            continue
        if clipped_left > cursor:
            remaining.append(text[cursor:clipped_left])
        cursor = max(cursor, clipped_right)
    if cursor < scope_end:
        remaining.append(text[cursor:scope_end])
    remaining.extend(
        replacement
        for left, right, replacement in replacement_intervals
        if scope_start <= left < right <= scope_end
    )
    return "".join(remaining).strip() == ""


def _load_bound_tokenizer(binding: Mapping[str, Any], model_id: str) -> Any:
    _require(
        binding.get("model_config_manifest_sha256") == MODEL_CONFIG_MANIFEST_SHA256
        and binding.get("model_id") == model_id,
        "tokenizer_model_config_binding_mismatch",
    )
    manifest_path = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff"
        / "g1"
        / "model_config_manifest.v1.json"
    )
    manifest = _load_json_object(manifest_path)
    model = next(entry for entry in manifest["models"] if entry["model_id"] == model_id)
    tokenizer = model["tokenizer"]
    _require(
        binding.get("tokenizer_revision") == tokenizer["revision"]
        and binding.get("tokenizer_artifact_set_sha256") == canonical_sha256(tokenizer["artifacts"])
        and binding.get("counting_call") == tokenizer["counting_call"]
        and binding.get("add_special_tokens") is False,
        "tokenizer_binding_mismatch",
    )
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(
            model["local_snapshot_reference"],
            revision=model["model_revision"],
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
    except Exception as error:
        raise CausalReplayRegistryError("tokenizer_load_failed", str(error)) from error


def _event(
    events: Mapping[str, Mapping[str, Any]], event_id: Any, expected_type: str
) -> Mapping[str, Any]:
    event = events.get(event_id)
    _require(isinstance(event, Mapping), "raw_event_missing", event_id=event_id)
    _require(event.get("event_type") == expected_type, "raw_event_type_mismatch", event_id=event_id)
    _require(isinstance(event.get("payload"), Mapping), "raw_event_payload_invalid")
    return event


def _validate_original_action(action: Any, prediction: Mapping[str, Any]) -> None:
    _require(
        prediction.get("parse_outcome") == "returned" and prediction.get("parse_exception") is None,
        "original_action_unparseable",
    )
    _require(isinstance(action, Mapping), "parsed_action_invalid")
    action_value = action.get("value")
    _require(
        isinstance(action_value, Mapping)
        and isinstance(action_value.get("action_type"), str)
        and bool(action_value["action_type"]),
        "parsed_action_value_invalid",
    )
    _require(
        action_value["action_type"] not in _FORBIDDEN_ACTION_TYPES,
        "parsed_action_placeholder_forbidden",
        action_type=action_value["action_type"],
    )


def _current_gui_ref(observation: Any) -> Mapping[str, Any]:
    _require(isinstance(observation, Mapping), "current_observation_invalid")
    screenshot = observation.get("screenshot")
    _require(isinstance(screenshot, Mapping), "current_screenshot_missing")
    reference = screenshot.get("pixel_blob")
    _require(isinstance(reference, Mapping), "current_gui_blob_missing")
    return reference


def _read_verified_blob(run_root: Path, reference: Any) -> bytes:
    _require(isinstance(reference, dict), "blob_reference_invalid")
    try:
        return BlobStore(run_root).read_bytes(reference)
    except Exception as error:
        raise CausalReplayRegistryError("blob_integrity_failure", str(error)) from error


def _blob_path(run_root: Path, reference: Mapping[str, Any]) -> Path:
    return _safe_child(
        run_root, _relative_path(reference.get("relative_path"), "blob.relative_path")
    )


def _resolve_base(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    _require(path.is_absolute(), "source_base_not_absolute", path=str(path))
    _require(not path.is_symlink(), "source_base_symlink", path=str(path))
    resolved = path.resolve(strict=True)
    _require(resolved.is_dir(), "source_base_not_directory", path=str(resolved))
    return resolved


def _resolve_pinned_file(value: Any) -> Path:
    text = _string(value, "pinned_file")
    supplied = Path(text)
    _require(supplied.is_absolute(), "pinned_file_not_absolute", path=text)
    _require(not supplied.is_symlink(), "input_symlink", path=text)
    resolved = supplied.resolve(strict=True)
    _require(resolved.is_file() and not resolved.is_symlink(), "pinned_file_invalid", path=text)
    return resolved


def _resolve_input(base: Path, value: Any) -> Path:
    text = _string(value, "input_path")
    supplied = Path(text)
    candidate = supplied if supplied.is_absolute() else base.joinpath(*PurePosixPath(text).parts)
    resolved = candidate.resolve(strict=True)
    _require(_is_within(resolved, base), "input_path_escape", path=text)
    _require(not candidate.is_symlink() and not resolved.is_symlink(), "input_symlink", path=text)
    _reject_symlink_components(base, resolved)
    return resolved


def _safe_child(root: Path, relative: Any) -> Path:
    text = _relative_path(relative, "relative_path")
    candidate = root.joinpath(*PurePosixPath(text).parts)
    resolved = candidate.resolve(strict=True)
    _require(_is_within(resolved, root), "relative_path_escape", path=text)
    _require(not candidate.is_symlink() and not resolved.is_symlink(), "input_symlink", path=text)
    _reject_symlink_components(root, resolved)
    return resolved


def _reject_symlink_components(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        _require(not current.is_symlink(), "input_symlink", path=str(current))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_regular(path: Path) -> bytes:
    _require(path.is_file() and not path.is_symlink(), "input_not_regular", path=str(path))
    return path.read_bytes()


def _load_json_object(path: Path) -> dict[str, Any]:
    value = _load_json_bytes(_read_regular(path), path)
    _require(isinstance(value, dict), "json_object_required", path=str(path))
    return value


def _load_json_bytes(data: bytes, path: Path) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicate_keys, parse_constant=_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CausalReplayRegistryError("json_invalid", str(error), path=str(path)) from error


def _load_jsonl_bytes(data: bytes, path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        _require(line.strip(), "jsonl_blank_line", path=str(path), line=line_number)
        try:
            value = json.loads(
                line, object_pairs_hook=_reject_duplicate_keys, parse_constant=_nonfinite
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CausalReplayRegistryError(
                "jsonl_invalid", str(error), path=str(path), line=line_number
            ) from error
        _require(isinstance(value, dict), "jsonl_object_required", path=str(path), line=line_number)
        records.append(value)
    _require(records, "jsonl_empty", path=str(path))
    return records


def _load_jsonl_prefix_through_event(
    data: bytes, path: Path, *, stop_event_id: str
) -> list[dict[str, Any]]:
    """Parse only the event prefix needed for an outcome-blind decision capsule."""

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        _require(line.strip(), "jsonl_blank_line", path=str(path), line=line_number)
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CausalReplayRegistryError(
                "jsonl_invalid",
                str(error),
                path=str(path),
                line=line_number,
            ) from error
        _require(isinstance(value, dict), "jsonl_object_required", path=str(path), line=line_number)
        records.append(value)
        if value.get("event_id") == stop_event_id:
            return records
    raise CausalReplayRegistryError(
        "raw_event_missing",
        "stop event not present in task stream",
        path=str(path),
        event_id=stop_event_id,
    )


def _load_canonical_jsonl(path: Path) -> list[dict[str, Any]]:
    return _load_canonical_jsonl_bytes(_read_regular(path), path)


def _load_canonical_jsonl_bytes(data: bytes, path: Path) -> list[dict[str, Any]]:
    records = []
    for line in data.splitlines(keepends=True):
        records.append(load_canonical_json_line(line))
    _require(records, "canonical_jsonl_empty", path=str(path))
    return records


def _load_canonical_object(path: Path) -> dict[str, Any]:
    data = _read_regular(path)
    _require(
        data == canonical_json_bytes(_load_json_bytes(data, path)),
        "json_not_canonical",
        path=str(path),
    )
    return _load_json_bytes(data, path)


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(record) for record in records)


def _file_summary(data: bytes, relative_path: str) -> dict[str, Any]:
    return {"relative_path": relative_path, "sha256": _digest(data), "byte_count": len(data)}


def _input_record(source_key: str, input_id: str, data: bytes) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "input_id": input_id,
        "sha256": _digest(data),
        "byte_count": len(data),
    }


def _verify_summary(data: bytes, summary: Any, *, path: str) -> None:
    _require(isinstance(summary, Mapping), "file_summary_invalid", path=path)
    _require(summary.get("byte_count") == len(data), "file_byte_count_drift", path=path)
    _require(summary.get("sha256") == _digest(data), "file_sha256_drift", path=path)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_hash(*parts: str) -> str:
    return _digest("\x1f".join(parts).encode("utf-8"))


def _json_clone(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value, newline=False))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_forbidden_input_name(value: str, *, path: str) -> None:
    lowered = value.lower()
    _require(
        not any(token in lowered for token in _FORBIDDEN_INPUT_TOKENS),
        "forbidden_future_evidence_input",
        path=path,
        value=value,
    )


def _relative_path(value: Any, path: str) -> str:
    text = _string(value, path)
    pure = PurePosixPath(text)
    _require(
        not pure.is_absolute()
        and pure.as_posix() == text
        and "\\" not in text
        and all(part not in {"", ".", ".."} for part in pure.parts),
        "relative_path_invalid",
        path=path,
    )
    return text


def _sha(value: Any, path: str) -> str:
    text = _string(value, path)
    _require(bool(_SHA_RE.fullmatch(text)), "sha256_invalid", path=path)
    return text


def _string(value: Any, path: str) -> str:
    _require(isinstance(value, str) and bool(value), "string_invalid", path=path)
    return value


def _positive_int(value: Any, path: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        "positive_int_invalid",
        path=path,
    )
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        "nonnegative_int_invalid",
        path=path,
    )
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    _require(isinstance(value, Mapping), "mapping_invalid", path=path)
    actual = set(value)
    _require(
        actual == expected,
        "object_keys_invalid",
        path=path,
        missing=sorted(expected - actual),
        extra=sorted(actual - expected),
    )


def _require(condition: bool, code: str, message: str | None = None, **context: Any) -> None:
    if not condition:
        raise CausalReplayRegistryError(code, message or code.replace("_", " "), **context)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or validate the outcome-blind G1.1 registry"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--source-base", required=True)
    build.add_argument("--config", required=True)
    build.add_argument("--output")
    build.add_argument("--dry-run", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--registry", required=True)
    validate.add_argument("--source-base", required=True)
    validate.add_argument("--config", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        sources = load_source_configuration(args.config)
        if args.command == "build":
            _require(args.dry_run or args.output, "output_required")
            artifacts = build_registry_artifacts(source_base=args.source_base, sources=sources)
            if not args.dry_run:
                write_registry_artifacts(artifacts, args.output)
            print(canonical_json_bytes(artifacts["validation"], newline=False).decode("utf-8"))
        else:
            validation = validate_registry_directory(
                args.registry,
                source_base=args.source_base,
                sources=sources,
            )
            print(canonical_json_bytes(validation, newline=False).decode("utf-8"))
    except CausalReplayRegistryError as error:
        print(
            canonical_json_bytes(
                {
                    "valid": False,
                    "error_code": error.code,
                    "message": str(error),
                    "context": error.context,
                },
                newline=False,
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 2
    return 0


__all__ = [
    "ARM_SCHEMA_VERSION",
    "BUILDER_VERSION",
    "CASE_SCHEMA_VERSION",
    "CASE_STATUS",
    "CausalReplayRegistryError",
    "PROTOCOL_VERSION",
    "RegistrySource",
    "build_registry_artifacts",
    "arm_order",
    "load_source_configuration",
    "main",
    "validate_admission_record",
    "validate_case_record",
    "validate_outcome_record",
    "validate_registry_directory",
    "validate_run_record",
    "write_registry_artifacts",
]
