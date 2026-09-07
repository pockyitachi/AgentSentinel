"""Independent, fail-closed post-run integrity gate for R2.5.

The live executor deliberately finishes resource cleanup and publishes its
terminal marker before this module may run.  This module does not own a model,
backend, secret, network transport, or GUI action capability.  It only reopens
already-published owner-only evidence, invokes the immutable Collector v1
integrity checker, and publishes a separately hash-bound acceptance artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Final, cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.integrity import (
    CHECKER_VERSION,
    IntegrityChecker,
    check_run_integrity,
)
from mobile_world.runtime.audit.serializer import ArtifactSerializer
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    authority_manifest_projection,
    authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    OwnedProcessIdentityV1,
    ProductionDriverError,
    ProductionModelHandoffEvidenceV1,
    ProductionModelStopEvidenceV1,
    ProductionPilotModelSwitchEvidenceV1,
    ProductionResourceStageEvidenceV1,
    ProductionResourceTopologyV1,
    ProductionSharedGpuAttestationV1,
    SharedGpuProcessEvidenceV1,
    production_model_handoff_evidence_projection,
    production_model_handoff_evidence_sha256,
    production_model_stop_evidence_projection,
    production_model_stop_evidence_sha256,
    production_pilot_model_switch_evidence_projection,
    production_pilot_model_switch_evidence_sha256,
    production_resource_stage_evidence_projection,
    production_resource_stage_evidence_sha256,
    production_shared_gpu_attestation_projection,
    production_shared_gpu_attestation_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import openai_stage_sha256
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LiveRubricError,
    parse_durable_live_attempt_receipt_projection_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    OFFICIAL_SUCCESS_METRIC_ID_V1,
    OFFICIAL_SUCCESS_OPERATOR_V1,
    OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1,
    FrozenPilotManifestV1,
    PilotHostV1,
    frozen_pilot_manifest_projection,
    frozen_pilot_manifest_sha256,
)

POST_RUN_INTEGRITY_SCHEMA_VERSION: Final = "mobileworld.runtime.sentinel-r2.5-post-run-integrity/v1"
POST_RUN_INTEGRITY_COMPLETION_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.5-post-run-integrity-completion/v1"
)
POST_RUN_INTEGRITY_WARNINGS_POLICY: Final = "REQUIRE_EMPTY"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_STAGE_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("RESOURCE_PREFLIGHT", "00-resource-preflight.json"),
    ("QWEN_LIVE_SMOKE", "01-qwen-live-smoke.json"),
    ("MAI_LIVE_SMOKE", "02-mai-live-smoke.json"),
    ("R25_PILOT", "03-r25-pilot.json"),
)
_SEQUENCE_FILES: Final[frozenset[str]] = frozenset(
    {
        "manifest-binding.json",
        *(filename for _, filename in _STAGE_FILES),
        "04-resource-cleanup.json",
        "terminal.json",
    }
)
_MAX_SEQUENCE_DOCUMENT_BYTES: Final = 512 * 1024 * 1024
_MAX_COLLECTOR_MANIFEST_BYTES: Final = 64 * 1024 * 1024
_MAX_INTEGRITY_REPORT_BYTES: Final = 16 * 1024 * 1024
_MAX_ACCEPTANCE_ARTIFACT_BYTES: Final = 256 * 1024 * 1024
_ACCEPTANCE_ARTIFACT_NAME: Final = "post-run-integrity.v1.json"
_ACCEPTANCE_COMPLETION_NAME: Final = "post-run-integrity.complete.v1.json"
_MAX_INLINE_UNIT_JOURNAL_BYTES: Final = 4 * 1024 * 1024
_CHECKER_REAP_RESERVE_NS: Final = 1_000_000_000
_OFFICIAL_RESULT_EVALUATOR_ID: Final = "mobileworld.task.official-success/v1"
_PRODUCTION_EVIDENCE_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.4-r2.5-production-driver-evidence/v1"
)
_UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.4-production-unit-evidence-blob-reference/v1"
)
_VALIDATED_UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.4-validated-unit-evidence-blob-reference/v1"
)
_UNIT_JOURNAL_BLOB_STORAGE: Final = "OWNER_ONLY_CONTENT_ADDRESSED_BLOB"
_UNIT_JOURNAL_BLOB_SUFFIX: Final = ".production-unit-evidence-blob.v1.json"
_PILOT_EVIDENCE_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.5-production-pilot-evidence/v2"
)
_FULL_SMOKE_EVIDENCE_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.5-production-smoke-evidence/v3"
)
_LIVE_EXECUTOR_BINDING_SCHEMA_VERSION: Final = (
    "mobileworld.runtime.sentinel-r2.4-r2.5-executor-binding/v1"
)
_SEQUENCE_RESULT_SCHEMA_VERSION: Final = "mobileworld.runtime.sentinel-r2.4-r2.5-sequence-result/v1"
_EXECUTOR_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "authorized_stages",
        "execution_scope",
        "factory_binding_sha256",
        "manifest_sha256",
        "max_model_switch_wall_time_seconds",
        "max_model_switches",
        "max_total_model_switch_wall_time_seconds",
        "pilot_switch_authority_sha256",
        "preflight_report_sha256",
        "pricing_sha256",
        "production_evidence_required",
        "resource_cleanup_upper_bound",
        "resource_cleanup_upper_bound_seconds",
        "resource_cleanup_upper_bound_sha256",
        "resource_topology",
        "run_id",
        "runtime_config_sha256",
        "schema_version",
        "sentinel_config_sha256",
        "source_commit",
    }
)
_STAGE_RECEIPT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_actions",
        "actor_calls",
        "completed_units",
        "cost_usd_micros",
        "evidence_sha256",
        "manifest_sha256",
        "openai_calls",
        "passed",
        "provider_final_request_proven",
        "stage",
        "wall_time_ms",
    }
)
_TERMINAL_CENSUS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_actions",
        "actor_calls",
        "cleanup_attempted",
        "cleanup_evidence_sha256",
        "cleanup_succeeded",
        "cleanup_wall_time_ms",
        "completed_stages",
        "cost_usd_micros",
        "openai_calls",
        "output_committed",
        "secret_leases_acquired",
        "secret_leases_closed",
        "stage_wall_time_ms",
        "state",
        "wall_time_ms",
    }
)
_VALIDATION_SEAL = object()


class R25PostRunIntegrityError(RuntimeError):
    """Stable, secret-free post-run integrity failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _BoundCollectorPath(type(Path())):  # type: ignore[misc]
    """A descriptor path whose root ``name`` remains the Collector run ID."""

    __slots__ = ("_bound_name", "_bound_root")

    @classmethod
    def build(cls, descriptor: int, run_id: str) -> _BoundCollectorPath:
        value = cls(f"/proc/self/fd/{descriptor}")
        value._bound_root = str(value)
        value._bound_name = run_id
        return value

    def with_segments(self, *pathsegments: str) -> _BoundCollectorPath:
        value = type(self)(*pathsegments)
        value._bound_root = self._bound_root
        value._bound_name = self._bound_name
        return value

    @property
    def name(self) -> str:
        if str(self) == self._bound_root:
            return cast(str, self._bound_name)
        return cast(str, super().name)


@dataclass(frozen=True, slots=True)
class PostRunIntegrityAuthorityV1:
    """Caller-known roots and the bounded read-only gate authority."""

    run_id: str
    authority_manifest_sha256: str
    preflight_report_sha256: str
    runtime_config_sha256: str
    pricing_sha256: str
    sentinel_config_sha256: str
    factory_binding_sha256: str
    run_manifest: R24R25RunAuthorityManifestV1
    source_commit: str
    pilot_manifest: FrozenPilotManifestV1
    pilot_manifest_sha256: str
    resolved_pilot_inputs_sha256: str
    backend_endpoint: str
    expected_cell_count: int
    max_sequence_wall_time_seconds: int
    max_wall_time_seconds: int
    production_audit_root: str | None = None

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or _SAFE_ID.fullmatch(self.run_id) is None:
            raise R25PostRunIntegrityError("INVALID_GATE_AUTHORITY", "run_id is invalid")
        for name in (
            "authority_manifest_sha256",
            "preflight_report_sha256",
            "runtime_config_sha256",
            "pricing_sha256",
            "sentinel_config_sha256",
            "factory_binding_sha256",
            "pilot_manifest_sha256",
            "resolved_pilot_inputs_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25PostRunIntegrityError("INVALID_GATE_AUTHORITY", f"{name} is invalid")
        if type(self.run_manifest) is not R24R25RunAuthorityManifestV1:
            raise R25PostRunIntegrityError("INVALID_GATE_AUTHORITY", "run manifest type differs")
        authorization = self.run_manifest.authorization
        if (
            authority_manifest_sha256(self.run_manifest) != self.authority_manifest_sha256
            or self.run_manifest.run_id != self.run_id
            or self.run_manifest.source_commit != self.source_commit
            or self.run_manifest.schema_version != R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2
            or self.run_manifest.authorization.status
            is not RunAuthorizationStatusV1.OWNER_AUTHORIZED
            or any(
                getattr(authorization, field) is not True
                for field in (
                    "network_allowed",
                    "gpu_allowed",
                    "docker_allowed",
                    "model_loading_allowed",
                    "backend_allowed",
                    "actor_model_calls_allowed",
                    "sentinel_provider_calls_allowed",
                    "pilot_gui_actions_allowed",
                )
            )
            or any(
                getattr(authorization, field) is not False
                for field in (
                    "smoke_gui_actions_allowed",
                    "merge_allowed",
                    "linear_update_allowed",
                    "frozen_artifact_mutation_allowed",
                )
            )
            or self.run_manifest.pilot != self.pilot_manifest
            or self.run_manifest.runtime_config_sha256 != self.runtime_config_sha256
            or self.run_manifest.pricing_sha256 != self.pricing_sha256
            or self.run_manifest.sentinel_config_sha256 != self.sentinel_config_sha256
            or self.run_manifest.resource_topology
            not in {
                "SINGLE_GPU_SEQUENTIAL_SHARED",
                "INDEPENDENT_GPU_CONCURRENT",
            }
            or self.run_manifest.max_sequence_wall_time_seconds
            != self.max_sequence_wall_time_seconds
            or self.run_manifest.max_post_run_integrity_wall_time_seconds
            != self.max_wall_time_seconds
            or self.run_manifest.output_root == ""
            or type(self.source_commit) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None
            or type(self.pilot_manifest) is not FrozenPilotManifestV1
            or frozen_pilot_manifest_sha256(self.pilot_manifest) != self.pilot_manifest_sha256
        ):
            raise R25PostRunIntegrityError(
                "INVALID_GATE_AUTHORITY", "source or frozen pilot preimage differs"
            )
        if (
            type(self.backend_endpoint) is not str
            or re.fullmatch(r"http://127\.0\.0\.1:(?:[1-9][0-9]{3,4})", self.backend_endpoint)
            is None
            or not 1_024 <= int(self.backend_endpoint.rsplit(":", 1)[1]) <= 65_535
        ):
            raise R25PostRunIntegrityError("INVALID_GATE_AUTHORITY", "backend endpoint is invalid")
        if (
            type(self.expected_cell_count) is not int
            or not 80 <= self.expected_cell_count <= 120
            or self.expected_cell_count % 4 != 0
            or self.expected_cell_count != len(self.pilot_manifest.cells)
            or type(self.max_sequence_wall_time_seconds) is not int
            or not 1 <= self.max_sequence_wall_time_seconds <= 604_800
            or type(self.max_wall_time_seconds) is not int
            or not 1 <= self.max_wall_time_seconds <= 86_400
            or self.max_wall_time_seconds > self.max_sequence_wall_time_seconds
        ):
            raise R25PostRunIntegrityError(
                "INVALID_GATE_AUTHORITY", "cell or wall bound is invalid"
            )
        if self.production_audit_root is not None and (
            type(self.production_audit_root) is not str
            or not Path(self.production_audit_root).is_absolute()
        ):
            raise R25PostRunIntegrityError(
                "INVALID_GATE_AUTHORITY", "production audit root is invalid"
            )


@dataclass(frozen=True, slots=True)
class CollectorRunLocatorV1:
    """One cell's durable, stage-bound Collector locator and semantic roots."""

    sequence_index: int
    manifest_sha256: str
    run_id: str
    task_id: str
    host: str
    arm: str
    unit_id: str
    run_root: str
    collector_run_id: str
    task_run_id: str
    manifest_final_path: str
    manifest_final_sha256: str
    manifest_final_byte_count: int
    unit_journal_sha256: str
    unit_journal: dict[str, JsonValue]
    unit_journal_validated_reference: dict[str, JsonValue] | None
    reset_evidence: dict[str, JsonValue]
    reset_evidence_sha256: str
    official_result: dict[str, JsonValue]
    official_result_evidence: dict[str, JsonValue]
    official_result_evidence_sha256: str
    cleanup_evidence: dict[str, JsonValue]
    cleanup_evidence_sha256: str
    decisions: list[JsonValue]
    census: dict[str, JsonValue]
    backend_endpoint: str
    resource_topology: str

    def __post_init__(self) -> None:
        if type(self.sequence_index) is not int or not 0 <= self.sequence_index < 120:
            raise R25PostRunIntegrityError("INVALID_COLLECTOR_LOCATOR", "cell index differs")
        for value, name in (
            (self.run_id, "run_id"),
            (self.task_id, "task_id"),
            (self.unit_id, "unit_id"),
            (self.collector_run_id, "collector_run_id"),
            (self.task_run_id, "task_run_id"),
        ):
            if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
                raise R25PostRunIntegrityError("INVALID_COLLECTOR_LOCATOR", f"{name} is invalid")
        if self.host not in {"QWEN3_VL", "MAI_UI"} or self.arm not in {
            "BASELINE",
            "JOINT_SENTINEL",
        }:
            raise R25PostRunIntegrityError("INVALID_COLLECTOR_LOCATOR", "host/arm differs")
        run_root = Path(self.run_root)
        manifest_path = Path(self.manifest_final_path)
        if (
            type(self.run_root) is not str
            or not run_root.is_absolute()
            or type(self.manifest_final_path) is not str
            or not manifest_path.is_absolute()
            or manifest_path != run_root / "manifest.final.json"
            or run_root.name != self.collector_run_id
        ):
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "Collector paths are not exact absolute locators"
            )
        for value, name in (
            (self.manifest_final_sha256, "manifest_final_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
            (self.unit_journal_sha256, "unit_journal_sha256"),
            (self.reset_evidence_sha256, "reset_evidence_sha256"),
            (self.official_result_evidence_sha256, "official_result_evidence_sha256"),
            (self.cleanup_evidence_sha256, "cleanup_evidence_sha256"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25PostRunIntegrityError("INVALID_COLLECTOR_LOCATOR", f"{name} is invalid")
        if (
            type(self.manifest_final_byte_count) is not int
            or not 1 <= self.manifest_final_byte_count <= _MAX_COLLECTOR_MANIFEST_BYTES
            or type(self.reset_evidence) is not dict
            or type(self.official_result) is not dict
            or type(self.official_result_evidence) is not dict
            or type(self.cleanup_evidence) is not dict
            or type(self.unit_journal) is not dict
            or type(self.decisions) is not list
            or type(self.census) is not dict
            or self.backend_endpoint == ""
            or self.resource_topology
            not in {"SINGLE_GPU_SEQUENTIAL_SHARED", "INDEPENDENT_GPU_CONCURRENT"}
            or (
                self.unit_journal_validated_reference is not None
                and type(self.unit_journal_validated_reference) is not dict
            )
            or _sha_json(cast(JsonValue, self.unit_journal)) != self.unit_journal_sha256
        ):
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "Collector evidence preimages differ"
            )


@dataclass(frozen=True, slots=True)
class SmokeCollectorRunLocatorV1:
    """One action-free smoke case's durable Collector locator."""

    sequence_index: int
    manifest_sha256: str
    run_id: str
    stage: str
    host: str
    mode: str
    case_id: str
    task_id: str
    unit_id: str
    run_root: str
    collector_run_id: str
    task_run_id: str
    manifest_final_path: str
    manifest_final_sha256: str
    manifest_final_byte_count: int
    unit_journal_sha256: str
    unit_journal: dict[str, JsonValue]
    decision: dict[str, JsonValue]
    census: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if type(self.sequence_index) is not int or not 0 <= self.sequence_index < 6:
            raise R25PostRunIntegrityError("INVALID_SMOKE_COLLECTOR_LOCATOR", "index differs")
        if (
            self.stage not in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}
            or self.host not in {"QWEN3_VL", "MAI_UI"}
            or self.mode not in {"OFF", "SHADOW", "ACTIVE"}
        ):
            raise R25PostRunIntegrityError(
                "INVALID_SMOKE_COLLECTOR_LOCATOR", "stage/host/mode differs"
            )
        for value, name in (
            (self.run_id, "run_id"),
            (self.case_id, "case_id"),
            (self.task_id, "task_id"),
            (self.unit_id, "unit_id"),
            (self.collector_run_id, "collector_run_id"),
            (self.task_run_id, "task_run_id"),
        ):
            if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
                raise R25PostRunIntegrityError(
                    "INVALID_SMOKE_COLLECTOR_LOCATOR", f"{name} is invalid"
                )
        run_root = Path(self.run_root)
        manifest_path = Path(self.manifest_final_path)
        if (
            not run_root.is_absolute()
            or not manifest_path.is_absolute()
            or manifest_path != run_root / "manifest.final.json"
            or run_root.name != self.collector_run_id
        ):
            raise R25PostRunIntegrityError(
                "INVALID_SMOKE_COLLECTOR_LOCATOR", "Collector paths differ"
            )
        for value, name in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.manifest_final_sha256, "manifest_final_sha256"),
            (self.unit_journal_sha256, "unit_journal_sha256"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25PostRunIntegrityError(
                    "INVALID_SMOKE_COLLECTOR_LOCATOR", f"{name} is invalid"
                )
        if (
            type(self.manifest_final_byte_count) is not int
            or not 1 <= self.manifest_final_byte_count <= _MAX_COLLECTOR_MANIFEST_BYTES
            or _sha_json(cast(JsonValue, self.unit_journal)) != self.unit_journal_sha256
        ):
            raise R25PostRunIntegrityError(
                "INVALID_SMOKE_COLLECTOR_LOCATOR", "durable smoke evidence differs"
            )


@dataclass(frozen=True, slots=True)
class ValidatedPostRunIntegrityArtifactV1:
    """Module-issued capability for one strict aggregate reopen."""

    artifact_path: str
    artifact_sha256: str
    ordered_collector_integrity_root_sha256: str
    authority_manifest_sha256: str
    pilot_manifest_sha256: str
    resolved_pilot_inputs_sha256: str
    preflight_report_sha256: str
    runtime_config_sha256: str
    pricing_sha256: str
    sentinel_config_sha256: str
    factory_binding_sha256: str
    backend_endpoint: str
    source_commit: str
    collector_run_count: int
    pilot_collector_run_count: int
    smoke_collector_run_count: int
    ordered_pilot_collector_integrity_root_sha256: str
    ordered_smoke_collector_integrity_root_sha256: str
    production_audit_root_identity_sha256: str | None
    _artifact_raw: bytes = dataclass_field(repr=False, compare=False)
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _VALIDATION_SEAL:
            raise R25PostRunIntegrityError(
                "UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
                "capability was not issued by the strict reopen validator",
            )
        if (
            type(self.artifact_path) is not str
            or not Path(self.artifact_path).is_absolute()
            or Path(self.artifact_path).name != _ACCEPTANCE_ARTIFACT_NAME
            or type(self.backend_endpoint) is not str
            or type(self.source_commit) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None
            or type(self.collector_run_count) is not int
            or not 86 <= self.collector_run_count <= 126
            or type(self.pilot_collector_run_count) is not int
            or not 80 <= self.pilot_collector_run_count <= 120
            or self.pilot_collector_run_count % 4 != 0
            or self.smoke_collector_run_count != 6
            or self.collector_run_count
            != self.pilot_collector_run_count + self.smoke_collector_run_count
            or type(self._artifact_raw) is not bytes
            or _sha_bytes(self._artifact_raw) != self.artifact_sha256
        ):
            raise R25PostRunIntegrityError(
                "UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
                "capability scalar fields differ",
            )
        artifact = _decode_canonical_json(
            self._artifact_raw,
            code="UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
        )
        expected_artifact_fields = {
            "authority_manifest_sha256": self.authority_manifest_sha256,
            "backend_endpoint": self.backend_endpoint,
            "collector_run_count": self.collector_run_count,
            "factory_binding_sha256": self.factory_binding_sha256,
            "ordered_collector_integrity_root_sha256": (
                self.ordered_collector_integrity_root_sha256
            ),
            "ordered_pilot_collector_integrity_root_sha256": (
                self.ordered_pilot_collector_integrity_root_sha256
            ),
            "ordered_smoke_collector_integrity_root_sha256": (
                self.ordered_smoke_collector_integrity_root_sha256
            ),
            "pilot_collector_run_count": self.pilot_collector_run_count,
            "pilot_manifest_sha256": self.pilot_manifest_sha256,
            "preflight_report_sha256": self.preflight_report_sha256,
            "pricing_sha256": self.pricing_sha256,
            "production_audit_root_identity_sha256": (self.production_audit_root_identity_sha256),
            "resolved_pilot_inputs_sha256": self.resolved_pilot_inputs_sha256,
            "runtime_config_sha256": self.runtime_config_sha256,
            "sentinel_config_sha256": self.sentinel_config_sha256,
            "smoke_collector_run_count": self.smoke_collector_run_count,
            "source_commit": self.source_commit,
        }
        if (
            artifact.get("schema_version") != POST_RUN_INTEGRITY_SCHEMA_VERSION
            or artifact.get("status") != "VALID"
            or any(
                artifact.get(field) != expected
                for field, expected in expected_artifact_fields.items()
            )
        ):
            raise R25PostRunIntegrityError(
                "UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
                "capability fields differ from its sealed artifact preimage",
            )
        for name in (
            "artifact_sha256",
            "ordered_collector_integrity_root_sha256",
            "ordered_pilot_collector_integrity_root_sha256",
            "ordered_smoke_collector_integrity_root_sha256",
            "authority_manifest_sha256",
            "pilot_manifest_sha256",
            "resolved_pilot_inputs_sha256",
            "preflight_report_sha256",
            "runtime_config_sha256",
            "pricing_sha256",
            "sentinel_config_sha256",
            "factory_binding_sha256",
        ):
            _require_sha256_value(
                getattr(self, name),
                code="UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
                name=name,
            )
        if self.production_audit_root_identity_sha256 is not None:
            _require_sha256_value(
                self.production_audit_root_identity_sha256,
                code="UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY",
                name="production_audit_root_identity_sha256",
            )


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_json(value: JsonValue) -> str:
    return _sha_bytes(canonical_json_bytes(value))


def _production_preimage(domain: str, value: JsonValue) -> dict[str, JsonValue]:
    return {
        "domain": domain,
        "schema_version": _PRODUCTION_EVIDENCE_SCHEMA_VERSION,
        "value": value,
    }


def _unwrap_production_preimage(
    envelope_value: object,
    *,
    expected_domain: str,
    expected_sha256: object,
    code: str,
    name: str,
) -> dict[str, JsonValue]:
    envelope = _object(envelope_value, code=code, name=name)
    if (
        set(envelope) != {"domain", "schema_version", "value"}
        or envelope.get("domain") != expected_domain
        or envelope.get("schema_version") != _PRODUCTION_EVIDENCE_SCHEMA_VERSION
        or type(expected_sha256) is not str
        or _SHA256.fullmatch(expected_sha256) is None
        or _sha_json(cast(JsonValue, envelope)) != expected_sha256
    ):
        raise R25PostRunIntegrityError(code, f"{name} production envelope differs")
    return _object(envelope.get("value"), code=code, name=f"{name}.value")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _decode_canonical_json(raw: bytes, *, code: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, "document is not strict JSON") from exc
    if type(value) is not dict or canonical_json_bytes(cast(JsonValue, value)) != raw:
        raise R25PostRunIntegrityError(code, "document is not canonical JSON")
    return cast(dict[str, JsonValue], value)


def _decode_canonical_json_line(raw: bytes, *, code: str) -> dict[str, JsonValue]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise R25PostRunIntegrityError(code, "document is not one canonical JSON line")
    return _decode_canonical_json(raw[:-1], code=code)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_owner_directory(
    path: Path,
    *,
    repository_root: Path,
    code: str,
    require_external: bool = True,
) -> tuple[Path, os.stat_result]:
    try:
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "directory is unavailable") from exc
    try:
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "repository root is unavailable") from exc
    if (
        resolved != absolute
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or (require_external and _is_within(resolved, repository))
    ):
        raise R25PostRunIntegrityError(code, "directory identity/ownership differs")
    return resolved, metadata


def _open_absolute_directory_chain_no_symlinks(
    path: Path,
) -> list[tuple[int, str, int, os.stat_result]]:
    """Open and retain every ancestor descriptor in one absolute path chain."""

    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise OSError("directory path must be a non-root canonical absolute path")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    chain = [(-1, "/", descriptor, os.fstat(descriptor))]
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise OSError("unsafe directory path component")
            child = os.open(part, flags, dir_fd=descriptor)
            child_metadata = os.fstat(child)
            chain.append((descriptor, part, child, child_metadata))
            descriptor = child
        return chain
    finally:
        if len(chain) != len(path.parts):
            for _, _, opened, _ in reversed(chain):
                os.close(opened)


@contextmanager
def _open_stable_owner_directory(
    path: Path,
    *,
    repository_root: Path,
    code: str,
) -> Iterator[tuple[Path, int, os.stat_result]]:
    resolved, path_metadata = _require_owner_directory(
        path, repository_root=repository_root, code=code
    )
    chain: list[tuple[int, str, int, os.stat_result]] = []
    try:
        chain = _open_absolute_directory_chain_no_symlinks(resolved)
        descriptor = chain[-1][2]
        descriptor_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(descriptor_metadata.st_mode)
            or stat.S_IMODE(descriptor_metadata.st_mode) != 0o700
            or descriptor_metadata.st_uid != os.geteuid()
            or descriptor_metadata.st_gid != os.getegid()
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise R25PostRunIntegrityError(code, "opened directory identity differs")
        yield resolved, descriptor, descriptor_metadata
        for parent_fd, name, child_fd, original in chain[1:]:
            rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            held = os.fstat(child_fd)
            if (
                stat.S_ISLNK(rebound.st_mode)
                or not stat.S_ISDIR(rebound.st_mode)
                or (rebound.st_dev, rebound.st_ino) != (original.st_dev, original.st_ino)
                or (held.st_dev, held.st_ino) != (original.st_dev, original.st_ino)
            ):
                raise R25PostRunIntegrityError(code, "directory ancestor identity changed")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        ):
            raise R25PostRunIntegrityError(code, "opened directory identity changed")
        try:
            current = resolved.lstat()
        except OSError as exc:
            raise R25PostRunIntegrityError(code, "directory path disappeared") from exc
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        ):
            raise R25PostRunIntegrityError(code, "directory path was replaced")
    except R25PostRunIntegrityError:
        raise
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "directory could not be opened safely") from exc
    finally:
        for _, _, opened, _ in reversed(chain):
            os.close(opened)


def _path_identity(path: Path, metadata: os.stat_result, *, kind: str) -> dict[str, JsonValue]:
    projection: dict[str, JsonValue] = {
        "canonical_path": str(path),
        "device": metadata.st_dev,
        "gid": metadata.st_gid,
        "inode": metadata.st_ino,
        "kind": kind,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }
    projection["identity_sha256"] = _sha_json(cast(JsonValue, projection))
    return projection


def _production_audit_root_identity_sha256(path: Path, metadata: os.stat_result) -> str:
    return _production_hash(
        "production-unit-evidence-owner-root-identity",
        cast(
            JsonValue,
            {
                "canonical_path_sha256": hashlib.sha256(os.fsencode(path)).hexdigest(),
                "device": metadata.st_dev,
                "gid": metadata.st_gid,
                "inode": metadata.st_ino,
                "uid": metadata.st_uid,
            },
        ),
    )


def _authority_audit_root_identity(
    authority: PostRunIntegrityAuthorityV1, *, repository_root: Path
) -> str | None:
    if authority.production_audit_root is None:
        return None
    with _open_stable_owner_directory(
        Path(authority.production_audit_root),
        repository_root=repository_root,
        code="INVALID_UNIT_JOURNAL",
    ) as (root, _, metadata):
        return _production_audit_root_identity_sha256(root, metadata)


def _read_owner_file(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
    require_canonical: bool = True,
) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise R25PostRunIntegrityError(code, "O_NOFOLLOW is unavailable")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise R25PostRunIntegrityError(code, "file metadata differs")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                raise R25PostRunIntegrityError(code, "file was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise R25PostRunIntegrityError(code, "file grew during read")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise R25PostRunIntegrityError(code, "file changed during read")
        raw = b"".join(chunks)
        if require_canonical:
            _decode_canonical_json(raw, code=code)
        return raw, after
    except R25PostRunIntegrityError:
        raise
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "file could not be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_owner_file_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int,
    code: str,
    require_canonical: bool = True,
) -> tuple[bytes, os.stat_result]:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise R25PostRunIntegrityError(code, "file name is not a safe leaf")
    return _read_owner_file(
        Path(f"/proc/self/fd/{directory_fd}") / name,
        maximum_bytes=maximum_bytes,
        code=code,
        require_canonical=require_canonical,
    )


def _read_owner_relative_file_at(
    directory_fd: int,
    relative_path: Path,
    *,
    maximum_bytes: int,
    code: str,
    require_canonical: bool,
) -> tuple[bytes, os.stat_result]:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise R25PostRunIntegrityError(code, "relative file path differs")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current_fd = directory_fd
    chain: list[tuple[int, str, int, os.stat_result]] = []
    try:
        for component in relative_path.parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=current_fd)
            metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child_fd)
                raise OSError("relative ancestor is not a directory")
            chain.append((current_fd, component, child_fd, metadata))
            current_fd = child_fd
        result = _read_owner_file_at(
            current_fd,
            relative_path.parts[-1],
            maximum_bytes=maximum_bytes,
            code=code,
            require_canonical=require_canonical,
        )
        for parent_fd, name, child_fd, original in chain:
            rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            held = os.fstat(child_fd)
            if (
                stat.S_ISLNK(rebound.st_mode)
                or not stat.S_ISDIR(rebound.st_mode)
                or (rebound.st_dev, rebound.st_ino) != (original.st_dev, original.st_ino)
                or (held.st_dev, held.st_ino) != (original.st_dev, original.st_ino)
            ):
                raise R25PostRunIntegrityError(code, "relative ancestor identity changed")
        return result
    except R25PostRunIntegrityError:
        raise
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "relative file could not be opened safely") from exc
    finally:
        for _, _, child_fd, _ in reversed(chain):
            os.close(child_fd)


def _write_fresh_owner_file_at(
    directory_fd: int,
    name: str,
    raw: bytes,
    *,
    code: str,
) -> os.stat_result:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise R25PostRunIntegrityError(code, "file name is not a safe leaf")
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        remaining = memoryview(raw)
        while remaining:
            count = os.write(descriptor, remaining)
            if count <= 0:
                raise OSError("short write")
            remaining = remaining[count:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or metadata.st_size != len(raw)
        ):
            raise OSError("published file metadata differs")
        os.fsync(directory_fd)
        return metadata
    except OSError as exc:
        raise R25PostRunIntegrityError(code, "fresh owner-only publication failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_time(deadline_ns: int) -> None:
    if time.monotonic_ns() >= deadline_ns:
        raise R25PostRunIntegrityError(
            "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED", "gate wall authority elapsed"
        )


def _object(value: object, *, code: str, name: str) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise R25PostRunIntegrityError(code, f"{name} must be an object")
    if any(type(key) is not str for key in cast(dict[object, object], value)):
        raise R25PostRunIntegrityError(code, f"{name} has a non-string key")
    return cast(dict[str, JsonValue], value)


def _list(value: object, *, code: str, name: str) -> list[JsonValue]:
    if type(value) is not list:
        raise R25PostRunIntegrityError(code, f"{name} must be an array")
    return cast(list[JsonValue], value)


_CENSUS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_actions",
        "actor_calls",
        "cost_usd_micros",
        "history_policy_openai_calls",
        "offline_rubric_evaluations",
        "openai_calls",
        "rubric_openai_calls",
        "wall_time_ms",
    }
)
_DECISION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_call_index",
        "actor_attempt_receipt_sha256",
        "case_execution_lease_sha256",
        "census",
        "exact_diff_sha256",
        "executed_action_sha256",
        "fallback_check",
        "fallback_reason",
        "final_request_sha256",
        "history_policy_attempt_receipt_sha256",
        "live_policy_authority_sha256",
        "live_policy_factory_binding_sha256",
        "logical_call_id",
        "parsed_action_sha256",
        "parser_result_sha256",
        "pre_provider_outcome",
        "pre_provider_status",
        "preflight_report_sha256",
        "provider_attempt_receipt_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "raw_request_sha256",
        "rubric_attempt_receipt_sha256s",
        "runtime_audit_detail_sha256",
        "sentinel_receipt_sha256",
    }
)


def _require_sha256_value(value: object, *, code: str, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R25PostRunIntegrityError(code, f"{name} is not SHA-256")
    return value


def _require_census_projection(
    value: object,
    *,
    code: str,
    name: str,
    require_one_actor_call: bool,
) -> dict[str, JsonValue]:
    census = _object(value, code=code, name=name)
    if (
        set(census) != _CENSUS_FIELDS
        or any(type(census[field]) is not int or cast(int, census[field]) < 0 for field in census)
        or (require_one_actor_call and census.get("actor_calls") != 1)
        or cast(int, census["offline_rubric_evaluations"]) > 1
        or cast(int, census["rubric_openai_calls"]) > 2
        or cast(int, census["history_policy_openai_calls"]) > 1
        or census.get("openai_calls")
        != cast(int, census["rubric_openai_calls"])
        + cast(int, census["history_policy_openai_calls"])
        or cast(int, census["actor_actions"]) > cast(int, census["actor_calls"])
    ):
        raise R25PostRunIntegrityError(code, f"{name} differs")
    return census


def _require_deadline_projection(value: object, *, code: str, name: str) -> dict[str, JsonValue]:
    deadline = _object(value, code=code, name=name)
    fields = {
        "attempt_termination_upper_bound_ns",
        "authority_deadline_monotonic_ns",
        "cleanup_deadline_monotonic_ns",
        "cleanup_grace_ns",
        "cleanup_within_owner_authority",
        "deadline_binding_sha256",
        "execution_deadline_monotonic_ns",
        "teardown_budget_ns",
        "teardown_budget_positive",
    }
    if set(deadline) != fields:
        raise R25PostRunIntegrityError(code, f"{name} fields differ")
    preimage = dict(deadline)
    claimed = preimage.pop("deadline_binding_sha256", None)
    for field in (
        "attempt_termination_upper_bound_ns",
        "authority_deadline_monotonic_ns",
        "cleanup_deadline_monotonic_ns",
        "cleanup_grace_ns",
        "execution_deadline_monotonic_ns",
        "teardown_budget_ns",
    ):
        if type(deadline.get(field)) is not int:
            raise R25PostRunIntegrityError(code, f"{name}.{field} differs")
    attempt = cast(int, deadline["attempt_termination_upper_bound_ns"])
    authority_deadline = cast(int, deadline["authority_deadline_monotonic_ns"])
    cleanup_deadline = cast(int, deadline["cleanup_deadline_monotonic_ns"])
    execution_deadline = cast(int, deadline["execution_deadline_monotonic_ns"])
    grace = cleanup_deadline - execution_deadline
    teardown = grace - attempt
    if (
        attempt < 0
        or execution_deadline <= 0
        or cleanup_deadline <= execution_deadline
        or authority_deadline < cleanup_deadline
        or deadline.get("cleanup_grace_ns") != grace
        or deadline.get("teardown_budget_ns") != teardown
        or deadline.get("cleanup_within_owner_authority") is not True
        or deadline.get("teardown_budget_positive") is not (teardown > 0)
        or teardown <= 0
        or claimed
        != _production_hash("production-unit-deadline-binding", cast(JsonValue, preimage))
    ):
        raise R25PostRunIntegrityError(code, f"{name} binding differs")
    return deadline


def _require_decision_projection(
    value: object,
    *,
    index: int,
    arm: str,
    expected_preflight_report_sha256: str,
    expected_factory_binding_sha256: str,
    allow_first_call_history_policy: bool = False,
) -> dict[str, JsonValue]:
    code = "INVALID_PILOT_STAGE"
    decision = _object(value, code=code, name="pilot.cell.decision")
    if set(decision) != _DECISION_FIELDS or decision.get("actor_call_index") != index:
        raise R25PostRunIntegrityError(code, "production decision fields/index differ")
    census = _require_census_projection(
        decision.get("census"),
        code=code,
        name="pilot.cell.decision.census",
        require_one_actor_call=True,
    )
    required_hash_fields = (
        "actor_attempt_receipt_sha256",
        "exact_diff_sha256",
        "final_request_sha256",
        "live_policy_factory_binding_sha256",
        "parsed_action_sha256",
        "parser_result_sha256",
        "preflight_report_sha256",
        "provider_attempt_receipt_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "raw_request_sha256",
        "runtime_audit_detail_sha256",
        "sentinel_receipt_sha256",
    )
    for field in required_hash_fields:
        _require_sha256_value(decision.get(field), code=code, name=f"decision.{field}")
    for field in (
        "case_execution_lease_sha256",
        "executed_action_sha256",
        "history_policy_attempt_receipt_sha256",
        "live_policy_authority_sha256",
    ):
        candidate = decision.get(field)
        if candidate is not None:
            _require_sha256_value(candidate, code=code, name=f"decision.{field}")
    rubric_hashes = _list(
        decision.get("rubric_attempt_receipt_sha256s"),
        code=code,
        name="decision.rubric_attempt_receipt_sha256s",
    )
    if len(rubric_hashes) > 2:
        raise R25PostRunIntegrityError(code, "decision rubric receipt census differs")
    for offset, value_sha256 in enumerate(rubric_hashes):
        _require_sha256_value(value_sha256, code=code, name=f"decision.rubric_receipt[{offset}]")
    logical_call_id = decision.get("logical_call_id")
    status = decision.get("pre_provider_status")
    outcome = decision.get("pre_provider_outcome")
    fallback_reason = decision.get("fallback_reason")
    fallback_check = decision.get("fallback_check")
    expected_outcomes = {
        "READY": {"READY"},
        "FALLBACK_ORIGINAL": {
            "GENERIC_FALLBACK_ORIGINAL",
            "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL",
        },
        "BYPASSED_ORIGINAL": {"BYPASSED_ORIGINAL"},
        "OFF": {"OFF"},
    }
    if (
        type(logical_call_id) is not str
        or _SAFE_ID.fullmatch(logical_call_id) is None
        or type(status) is not str
        or status not in expected_outcomes
        or outcome not in expected_outcomes[status]
        or decision.get("preflight_report_sha256") != expected_preflight_report_sha256
        or decision.get("live_policy_factory_binding_sha256") != expected_factory_binding_sha256
        or decision.get("provider_request_sha256") != decision.get("final_request_sha256")
    ):
        raise R25PostRunIntegrityError(code, "decision authority/request binding differs")
    if status in {"READY", "OFF"}:
        fallback_valid = fallback_reason is None and fallback_check is None
    elif status == "FALLBACK_ORIGINAL":
        fallback_valid = (
            type(fallback_reason) is str
            and bool(fallback_reason)
            and type(fallback_check) is str
            and _SAFE_ID.fullmatch(fallback_check) is not None
        )
    else:
        fallback_valid = (
            fallback_reason is None
            and type(fallback_check) is str
            and _SAFE_ID.fullmatch(fallback_check) is not None
        )
    semantic_receipts = bool(rubric_hashes) or (
        decision.get("history_policy_attempt_receipt_sha256") is not None
    )
    semantic_authority = (
        decision.get("live_policy_authority_sha256") is not None
        and decision.get("case_execution_lease_sha256") is not None
    )
    if (
        not fallback_valid
        or semantic_receipts is not semantic_authority
        or cast(int, census["rubric_openai_calls"]) != len(rubric_hashes)
        or cast(int, census["history_policy_openai_calls"])
        != int(decision.get("history_policy_attempt_receipt_sha256") is not None)
        or (
            cast(int, census["actor_actions"]) == 0
            and decision.get("executed_action_sha256") is not None
        )
        or (
            cast(int, census["actor_actions"]) == 1
            and decision.get("executed_action_sha256") != decision.get("parsed_action_sha256")
        )
    ):
        raise R25PostRunIntegrityError(code, "decision receipt/action binding differs")
    if arm == "BASELINE" and (
        status != "OFF"
        or outcome != "OFF"
        or semantic_receipts
        or decision.get("live_policy_authority_sha256") is not None
        or decision.get("case_execution_lease_sha256") is not None
        or decision.get("raw_request_sha256") != decision.get("final_request_sha256")
        or census.get("openai_calls") != 0
        or census.get("offline_rubric_evaluations") != 0
    ):
        raise R25PostRunIntegrityError(code, "baseline decision is not exact OFF")
    if arm == "JOINT_SENTINEL":
        expected_rubric_calls = 2 if index == 1 else 1
        expected_history_calls = (
            int(decision.get("history_policy_attempt_receipt_sha256") is not None)
            if index == 1 and allow_first_call_history_policy
            else (0 if index == 1 else 1)
        )
        generic_original_fallback = (
            status == "FALLBACK_ORIGINAL"
            and outcome == "GENERIC_FALLBACK_ORIGINAL"
            and fallback_reason == "POLICY_EXCEPTION"
            and fallback_check == "policy_exception"
            and decision.get("raw_request_sha256") == decision.get("final_request_sha256")
        )
        if (
            census.get("offline_rubric_evaluations") != 0
            or census.get("rubric_openai_calls") != expected_rubric_calls
            or census.get("history_policy_openai_calls") != expected_history_calls
            or census.get("openai_calls") != expected_rubric_calls + expected_history_calls
            or len(rubric_hashes) != expected_rubric_calls
            or int(decision.get("history_policy_attempt_receipt_sha256") is not None)
            != expected_history_calls
            or (
                expected_history_calls == 0
                and (
                    status != "FALLBACK_ORIGINAL"
                    or outcome != "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL"
                    or fallback_reason != "HISTORY_EXTRACTION_FAILURE"
                    or fallback_check != "r2_4_no_history_r21_v1_compatibility"
                )
            )
            or (
                expected_history_calls == 1
                and not generic_original_fallback
                and (status != "READY" or outcome != "READY")
            )
        ):
            raise R25PostRunIntegrityError(
                code, "joint decision rubric/history call topology differs"
            )
    return decision


def _require_projection_list_hash(
    journal: dict[str, JsonValue], *, field: str, domain: str
) -> list[JsonValue]:
    values = _list(journal.get(field), code="INVALID_UNIT_JOURNAL", name=f"unit_journal.{field}")
    if journal.get(f"{field}_sha256") != _production_hash(domain, cast(JsonValue, values)):
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", f"unit journal {field} hash differs")
    return values


def _validate_terminal_audit_records(
    values: list[JsonValue],
    *,
    decisions: list[JsonValue],
    authority: PostRunIntegrityAuthorityV1,
) -> None:
    decision_objects = [
        _object(item, code="INVALID_UNIT_JOURNAL", name="cell.decision") for item in decisions
    ]
    decision_ids = [item.get("logical_call_id") for item in decision_objects]
    if (
        not decision_ids
        or any(type(item) is not str or not item for item in decision_ids)
        or len(set(decision_ids)) != len(decision_ids)
        or len(values) != len(decision_ids)
    ):
        raise R25PostRunIntegrityError(
            "INVALID_UNIT_JOURNAL", "terminal audit/decision census differs"
        )
    terminal_ids: list[JsonValue] = []
    for record_value in values:
        record = _object(
            record_value,
            code="INVALID_UNIT_JOURNAL",
            name="unit_journal.terminal_audit_record",
        )
        if set(record) != {
            "attempt_journal_failure_code",
            "canonical_evidence_sha256",
            "kind",
            "live_attempt_receipt_sha256s",
            "live_attempt_receipts",
            "receipt",
            "receipt_sha256",
        }:
            raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "terminal audit fields differ")
        preimage = dict(record)
        claimed = preimage.pop("canonical_evidence_sha256")
        if (
            claimed != _production_hash("production-unit-terminal-audit", cast(JsonValue, preimage))
            or record.get("kind") != "COMPLETED"
            or record.get("attempt_journal_failure_code") is not None
        ):
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "terminal audit outcome/hash differs"
            )
        attempts = _list(
            record.get("live_attempt_receipts"),
            code="INVALID_UNIT_JOURNAL",
            name="terminal.live_attempt_receipts",
        )
        hashes = _list(
            record.get("live_attempt_receipt_sha256s"),
            code="INVALID_UNIT_JOURNAL",
            name="terminal.live_attempt_receipt_sha256s",
        )
        attempt_objects = [
            _object(
                attempt,
                code="INVALID_UNIT_JOURNAL",
                name="terminal.live_attempt_receipt",
            )
            for attempt in attempts
        ]
        try:
            typed_attempts = [
                parse_durable_live_attempt_receipt_projection_v1(item) for item in attempt_objects
            ]
        except (LiveRubricError, TypeError, ValueError) as exc:
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "terminal attempt is not a durable production receipt"
            ) from exc
        expected_stage_roots = {
            stage.role.value: openai_stage_sha256(stage)
            for stage in authority.run_manifest.openai_stages
        }
        if (
            any(type(item) is not str or _SHA256.fullmatch(item) is None for item in hashes)
            or hashes != [_sha_json(item) for item in attempts]
            or len(set(cast(list[str], hashes))) != len(hashes)
            or any(item.get("role") not in {"RUBRIC", "HISTORY_POLICY"} for item in attempt_objects)
            or any(
                receipt.manifest_sha256 != authority.authority_manifest_sha256
                or receipt.preflight_sha256 != authority.preflight_report_sha256
                or receipt.pricing_binding_sha256 != authority.pricing_sha256
                or receipt.stage_sha256 != expected_stage_roots.get(receipt.role.value)
                or receipt.status.value != "COMPLETED"
                or receipt.execution_kind.value != "OPENAI_RESPONSES_CHILD_PROCESS"
                for receipt in typed_attempts
            )
        ):
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "terminal live attempt census/hash differs"
            )
        receipt = _object(
            record.get("receipt"), code="INVALID_UNIT_JOURNAL", name="terminal.receipt"
        )
        if record.get("receipt_sha256") != _sha_json(cast(JsonValue, receipt)):
            raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "terminal receipt hash differs")
        terminal_ids.append(receipt.get("logical_call_id"))
        matching = next(
            (item for item in decision_objects if item.get("logical_call_id") == terminal_ids[-1]),
            None,
        )
        decision_census = (
            None
            if matching is None
            else _object(
                matching.get("census"),
                code="INVALID_UNIT_JOURNAL",
                name="decision.census",
            )
        )
        if (
            matching is None
            or decision_census is None
            or receipt.get("live_openai_calls") != decision_census.get("openai_calls")
            or receipt.get("action_executed") is not (decision_census.get("actor_actions") == 1)
            or receipt.get("detail_sha256") != matching.get("runtime_audit_detail_sha256")
            or receipt.get("sentinel_receipt_sha256") != matching.get("sentinel_receipt_sha256")
            or receipt.get("parser_result_sha256") != matching.get("parser_result_sha256")
        ):
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "terminal receipt/decision binding differs"
            )
        rubric_hashes = [
            hash_value
            for attempt, hash_value in zip(attempt_objects, hashes, strict=True)
            if attempt.get("role") == "RUBRIC"
        ]
        history_hashes = [
            hash_value
            for attempt, hash_value in zip(attempt_objects, hashes, strict=True)
            if attempt.get("role") == "HISTORY_POLICY"
        ]
        if (
            matching.get("rubric_attempt_receipt_sha256s") != rubric_hashes
            or matching.get("history_policy_attempt_receipt_sha256")
            != (history_hashes[0] if len(history_hashes) == 1 else None)
            or len(history_hashes) > 1
            or [item.get("role") for item in attempt_objects]
            != ["RUBRIC"] * len(rubric_hashes) + (["HISTORY_POLICY"] if history_hashes else [])
            or any(
                item.get("logical_call_id") != matching.get("logical_call_id")
                for item in attempt_objects
            )
            or any(
                receipt.logical_call_id != matching.get("logical_call_id")
                or receipt.authority_sha256 != matching.get("live_policy_authority_sha256")
                or receipt.case_execution_lease_sha256
                != matching.get("case_execution_lease_sha256")
                or receipt.actor_request_sha256 != matching.get("raw_request_sha256")
                for receipt in typed_attempts
            )
        ):
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "live attempts differ from decision bindings"
            )
    if terminal_ids != decision_ids:
        raise R25PostRunIntegrityError(
            "INVALID_UNIT_JOURNAL", "terminal audit order differs from actor decisions"
        )


def _validate_full_unit_journal(
    journal: dict[str, JsonValue],
    *,
    locator: CollectorRunLocatorV1,
    authority: PostRunIntegrityAuthorityV1,
) -> dict[str, JsonValue]:
    if set(journal) != {
        "cleanup_recovery_outcome",
        "cleanup_recovery_outcome_sha256",
        "collector_run_binding",
        "collector_run_binding_sha256",
        "completed_decisions",
        "completed_decisions_sha256",
        "official_result_evidence",
        "official_result_evidence_sha256",
        "reset_evidence",
        "reset_evidence_sha256",
        "resource_dispatch_records",
        "resource_dispatch_records_sha256",
        "run_fatal_state",
        "run_fatal_state_sha256",
        "terminal_audit_records",
        "terminal_audit_records_sha256",
        "unit_deadline",
        "unit_id",
    }:
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "unit journal fields differ")
    decisions = _require_projection_list_hash(
        journal, field="completed_decisions", domain="production-unit-decision-journal"
    )
    terminals = _require_projection_list_hash(
        journal,
        field="terminal_audit_records",
        domain="production-unit-terminal-audit-journal",
    )
    dispatches = _require_projection_list_hash(
        journal,
        field="resource_dispatch_records",
        domain="production-unit-resource-dispatch-journal",
    )
    expected_collector_binding: dict[str, JsonValue] = {
        "collector_manifest_capture_complete": True,
        "collector_manifest_final_byte_count": locator.manifest_final_byte_count,
        "collector_manifest_final_path": locator.manifest_final_path,
        "collector_manifest_final_sha256": locator.manifest_final_sha256,
        "collector_manifest_runtime_status": "completed",
        "collector_run_id": locator.collector_run_id,
        "collector_run_root": locator.run_root,
        "collector_task_run_id": locator.task_run_id,
    }
    cleanup_outcome = _object(
        journal.get("cleanup_recovery_outcome"),
        code="INVALID_UNIT_JOURNAL",
        name="unit_journal.cleanup_recovery_outcome",
    )
    unit_deadline = _require_deadline_projection(
        journal.get("unit_deadline"),
        code="INVALID_UNIT_JOURNAL",
        name="unit_journal.unit_deadline",
    )
    cleanup_deadline = _require_deadline_projection(
        locator.cleanup_evidence.get("deadline_binding"),
        code="INVALID_UNIT_JOURNAL",
        name="cleanup_evidence.deadline_binding",
    )
    if (
        journal.get("unit_id") != locator.unit_id
        or decisions != locator.decisions
        or not dispatches
        or journal.get("run_fatal_state") is not None
        or journal.get("run_fatal_state_sha256") is not None
        or journal.get("reset_evidence") != locator.reset_evidence
        or journal.get("reset_evidence_sha256") != locator.reset_evidence_sha256
        or journal.get("official_result_evidence") != locator.official_result_evidence
        or journal.get("official_result_evidence_sha256") != locator.official_result_evidence_sha256
        or journal.get("collector_run_binding") != expected_collector_binding
        or journal.get("collector_run_binding_sha256")
        != _production_hash(
            "production-collector-run-binding", cast(JsonValue, expected_collector_binding)
        )
        or set(cleanup_outcome)
        != {
            "initialization_permitted",
            "message_sha256",
            "outcome",
            "request_dispatched",
            "teardown_attempted",
        }
        or cleanup_outcome.get("initialization_permitted") is not False
        or cleanup_outcome.get("outcome") != "SUCCEEDED"
        or cleanup_outcome.get("request_dispatched") is not True
        or cleanup_outcome.get("teardown_attempted") is not True
        or type(cleanup_outcome.get("message_sha256")) is not str
        or _SHA256.fullmatch(cast(str, cleanup_outcome["message_sha256"])) is None
        or journal.get("cleanup_recovery_outcome_sha256")
        != _production_hash(
            "production-unit-cleanup-recovery-outcome", cast(JsonValue, cleanup_outcome)
        )
        or unit_deadline != cleanup_deadline
    ):
        raise R25PostRunIntegrityError(
            "INVALID_UNIT_JOURNAL", "unit journal canonical bindings differ"
        )
    _validate_terminal_audit_records(terminals, decisions=decisions, authority=authority)
    census_fields = {
        "actor_actions",
        "actor_calls",
        "cost_usd_micros",
        "history_policy_openai_calls",
        "offline_rubric_evaluations",
        "openai_calls",
        "rubric_openai_calls",
        "wall_time_ms",
    }
    summed = {field: 0 for field in census_fields}
    for decision_value in decisions:
        decision = _object(
            decision_value, code="INVALID_UNIT_JOURNAL", name="unit_journal.decision"
        )
        census = _object(
            decision.get("census"), code="INVALID_UNIT_JOURNAL", name="decision.census"
        )
        if set(census) != census_fields or any(type(census[field]) is not int for field in census):
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "decision census fields/types differ"
            )
        for field in census_fields:
            summed[field] += cast(int, census[field])
    expected_census: dict[str, JsonValue] = dict(summed)
    expected_census["wall_time_ms"] = locator.census.get("wall_time_ms")
    if (
        set(locator.census) != census_fields
        or any(type(locator.census[field]) is not int for field in locator.census)
        or any(
            locator.census[field] != expected_census[field]
            for field in census_fields
            if field != "wall_time_ms"
        )
        or cast(int, locator.census["wall_time_ms"]) < summed["wall_time_ms"]
    ):
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "cell and decision census differ")
    return journal


def validate_durable_unit_journal_projection_v1(
    journal: dict[str, JsonValue],
    *,
    locator: CollectorRunLocatorV1,
    authority: PostRunIntegrityAuthorityV1,
    production_audit_root: Path | None,
    repository_root: Path,
) -> dict[str, JsonValue]:
    """Strictly resolve and validate one inline or owner-only CAS unit journal."""

    journal_raw = canonical_json_bytes(cast(JsonValue, journal))
    if _sha_bytes(journal_raw) != locator.unit_journal_sha256:
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "journal hash differs")
    if journal.get("schema_version") != _UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION:
        if len(journal_raw) > _MAX_INLINE_UNIT_JOURNAL_BYTES:
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "inline journal exceeds its bound"
            )
        if locator.unit_journal_validated_reference is not None:
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "inline journal has an orphan CAS validation"
            )
        return _validate_full_unit_journal(journal, locator=locator, authority=authority)
    if production_audit_root is None:
        raise R25PostRunIntegrityError(
            "INVALID_UNIT_JOURNAL", "CAS journal lacks its authority-pinned audit root"
        )
    reference = journal
    validation = locator.unit_journal_validated_reference
    blob = _object(reference.get("blob"), code="INVALID_UNIT_JOURNAL", name="unit_journal.blob")
    if type(validation) is not dict:
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "CAS validation is absent")
    reference_preimage = dict(reference)
    reference_sha256 = reference_preimage.pop("reference_sha256", None)
    if (
        set(reference) != {"blob", "reference_sha256", "schema_version", "storage", "unit_id"}
        or reference.get("storage") != _UNIT_JOURNAL_BLOB_STORAGE
        or reference.get("unit_id") != locator.unit_id
        or reference_sha256 != _sha_json(cast(JsonValue, reference_preimage))
        or set(blob) != {"algorithm", "byte_count", "safe_locator", "sha256"}
        or blob.get("algorithm") != "sha256"
        or type(blob.get("sha256")) is not str
        or _SHA256.fullmatch(cast(str, blob["sha256"])) is None
        or blob.get("safe_locator") != f"{blob['sha256']}{_UNIT_JOURNAL_BLOB_SUFFIX}"
        or type(blob.get("byte_count")) is not int
        or cast(int, blob["byte_count"]) <= _MAX_INLINE_UNIT_JOURNAL_BYTES
        or set(validation)
        != {
            "blob",
            "expected_unit_id",
            "owner_audit_root_identity_sha256",
            "reference_preimage_sha256",
            "reference_sha256",
            "resource_dispatch_record_count",
            "resource_dispatch_records_sha256",
            "schema_version",
            "validation_status",
        }
        or validation.get("schema_version") != _VALIDATED_UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION
        or validation.get("validation_status") != "EXACT_OWNER_ONLY_READBACK"
        or validation.get("expected_unit_id") != locator.unit_id
        or validation.get("reference_preimage_sha256") != locator.unit_journal_sha256
        or validation.get("reference_sha256") != reference_sha256
        or validation.get("blob") != blob
    ):
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "CAS reference differs")
    with _open_stable_owner_directory(
        production_audit_root,
        repository_root=repository_root,
        code="INVALID_UNIT_JOURNAL",
    ) as (audit_root, audit_root_fd, audit_metadata):
        root_identity_sha256 = _production_audit_root_identity_sha256(audit_root, audit_metadata)
        if validation.get("owner_audit_root_identity_sha256") != root_identity_sha256:
            raise R25PostRunIntegrityError(
                "INVALID_UNIT_JOURNAL", "CAS owner root identity differs"
            )
        blob_raw, _ = _read_owner_file_at(
            audit_root_fd,
            cast(str, blob["safe_locator"]),
            maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
            code="INVALID_UNIT_JOURNAL",
            require_canonical=False,
        )
    if len(blob_raw) != blob["byte_count"] or _sha_bytes(blob_raw) != blob["sha256"]:
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "CAS blob differs")
    full = _decode_canonical_json(blob_raw, code="INVALID_UNIT_JOURNAL")
    validated = _validate_full_unit_journal(full, locator=locator, authority=authority)
    dispatches = _list(
        validated.get("resource_dispatch_records"),
        code="INVALID_UNIT_JOURNAL",
        name="unit_journal.resource_dispatch_records",
    )
    if validation.get("resource_dispatch_record_count") != len(dispatches) or validation.get(
        "resource_dispatch_records_sha256"
    ) != validated.get("resource_dispatch_records_sha256"):
        raise R25PostRunIntegrityError("INVALID_UNIT_JOURNAL", "CAS dispatch validation differs")
    return validated


def validate_pilot_stage_durable_evidence_projection_v2(
    pilot: dict[str, JsonValue],
    *,
    expected_run_manifest: R24R25RunAuthorityManifestV1,
    expected_manifest_sha256: str,
    expected_resolved_pilot_inputs_sha256: str,
    expected_backend_endpoint: str,
    expected_preflight_report_sha256: str,
    expected_factory_binding_sha256: str,
) -> dict[str, JsonValue]:
    """Pure CPU validation of the closed production-v2 pilot projection."""

    if type(expected_run_manifest) is not R24R25RunAuthorityManifestV1:
        raise R25PostRunIntegrityError("INVALID_PILOT_STAGE", "expected run manifest type differs")
    expected_run_id = expected_run_manifest.run_id
    expected_pilot_manifest = expected_run_manifest.pilot
    expected_pilot_manifest_sha256 = frozen_pilot_manifest_sha256(expected_pilot_manifest)
    expected_cells = expected_pilot_manifest.cells
    expected_cell_count = len(expected_cells)
    run_manifest_projection = authority_manifest_projection(expected_run_manifest)
    actor_resources = _list(
        run_manifest_projection.get("actor_resources"),
        code="INVALID_PILOT_STAGE",
        name="run_manifest.actor_resources",
    )
    expected_resource_hashes: dict[str, str] = {}
    actor_resource_matrix: list[JsonValue] = []
    for resource_value in actor_resources:
        resource = _object(
            resource_value,
            code="INVALID_PILOT_STAGE",
            name="run_manifest.actor_resource",
        )
        host = resource.get("host")
        if type(host) is not str or host in expected_resource_hashes:
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "run manifest actor resource matrix differs"
            )
        resource_sha256 = _production_hash("actor-resource", cast(JsonValue, resource))
        expected_resource_hashes[host] = resource_sha256
        actor_resource_matrix.append(
            cast(JsonValue, {"host": host, "resource_sha256": resource_sha256})
        )
    expected_actor_resources_sha256 = _production_hash(
        "actor-resource-matrix", cast(JsonValue, actor_resource_matrix)
    )
    openai_stages = _list(
        run_manifest_projection.get("openai_stages"),
        code="INVALID_PILOT_STAGE",
        name="run_manifest.openai_stages",
    )
    policy_stages = [
        _object(
            value,
            code="INVALID_PILOT_STAGE",
            name="run_manifest.openai_stage",
        )
        for value in openai_stages
        if _object(
            value,
            code="INVALID_PILOT_STAGE",
            name="run_manifest.openai_stage",
        ).get("role")
        == "HISTORY_POLICY"
    ]
    if len(policy_stages) != 1:
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "run manifest history policy stage differs"
        )
    expected_history_policy_stage_sha256 = _production_hash(
        "history-policy-stage", cast(JsonValue, policy_stages[0])
    )
    if (
        type(expected_manifest_sha256) is not str
        or _SHA256.fullmatch(expected_manifest_sha256) is None
        or authority_manifest_sha256(expected_run_manifest) != expected_manifest_sha256
        or type(expected_resolved_pilot_inputs_sha256) is not str
        or _SHA256.fullmatch(expected_resolved_pilot_inputs_sha256) is None
        or type(expected_preflight_report_sha256) is not str
        or _SHA256.fullmatch(expected_preflight_report_sha256) is None
        or type(expected_factory_binding_sha256) is not str
        or _SHA256.fullmatch(expected_factory_binding_sha256) is None
        or type(expected_backend_endpoint) is not str
        or re.fullmatch(r"http://127\.0\.0\.1:(?:[1-9][0-9]{3,4})", expected_backend_endpoint)
        is None
        or type(expected_run_id) is not str
        or _SAFE_ID.fullmatch(expected_run_id) is None
        or set(expected_resource_hashes) != {"QWEN3_VL", "MAI_UI"}
        or not 80 <= expected_cell_count <= 120
        or expected_cell_count % 4 != 0
    ):
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "expected pilot authority inputs differ"
        )
    journal_authority = PostRunIntegrityAuthorityV1(
        run_id=expected_run_id,
        authority_manifest_sha256=expected_manifest_sha256,
        preflight_report_sha256=expected_preflight_report_sha256,
        runtime_config_sha256=cast(str, expected_run_manifest.runtime_config_sha256),
        pricing_sha256=cast(str, expected_run_manifest.pricing_sha256),
        sentinel_config_sha256=cast(str, expected_run_manifest.sentinel_config_sha256),
        factory_binding_sha256=expected_factory_binding_sha256,
        run_manifest=expected_run_manifest,
        source_commit=expected_run_manifest.source_commit,
        pilot_manifest=expected_pilot_manifest,
        pilot_manifest_sha256=expected_pilot_manifest_sha256,
        resolved_pilot_inputs_sha256=expected_resolved_pilot_inputs_sha256,
        backend_endpoint=expected_backend_endpoint,
        expected_cell_count=expected_cell_count,
        max_sequence_wall_time_seconds=(expected_run_manifest.max_sequence_wall_time_seconds),
        max_wall_time_seconds=cast(
            int, expected_run_manifest.max_post_run_integrity_wall_time_seconds
        ),
    )
    if set(pilot) != {
        "actor_resources_sha256",
        "cells",
        "census",
        "history_policy_stage_sha256",
        "manifest_sha256",
        "pilot_manifest_sha256",
        "run_id",
        "schema_version",
    }:
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "production pilot stage fields differ"
        )
    for name in (
        "actor_resources_sha256",
        "history_policy_stage_sha256",
        "pilot_manifest_sha256",
    ):
        if type(pilot.get(name)) is not str or _SHA256.fullmatch(cast(str, pilot[name])) is None:
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", f"production pilot {name} differs"
            )
    if (
        pilot.get("schema_version") != _PILOT_EVIDENCE_SCHEMA_VERSION
        or pilot.get("manifest_sha256") != expected_manifest_sha256
        or pilot.get("pilot_manifest_sha256") != expected_pilot_manifest_sha256
        or pilot.get("actor_resources_sha256") != expected_actor_resources_sha256
        or pilot.get("history_policy_stage_sha256") != expected_history_policy_stage_sha256
        or pilot.get("run_id") != expected_run_id
    ):
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "production pilot authority binding differs"
        )
    cells = _list(pilot.get("cells"), code="INVALID_PILOT_STAGE", name="pilot.cells")
    stage_census = _object(pilot.get("census"), code="INVALID_PILOT_STAGE", name="pilot.census")
    census_fields = {
        "actor_actions",
        "actor_calls",
        "cost_usd_micros",
        "history_policy_openai_calls",
        "offline_rubric_evaluations",
        "openai_calls",
        "rubric_openai_calls",
        "wall_time_ms",
    }
    base_cell_fields = {
        "actor_resource_sha256",
        "arm",
        "census",
        "cleanup_receipt_sha256",
        "decisions",
        "effective_reset_state_sha256",
        "history_policy_stage_sha256",
        "host",
        "manifest_sha256",
        "official_result",
        "reset_receipt_sha256",
        "reset_seed",
        "run_id",
        "sentinel_mode",
        "sequence_index",
        "task_id",
        "task_parameters_sha256",
    }
    durable_fields = {
        "cleanup_evidence",
        "cleanup_evidence_sha256",
        "collector_run_locator",
        "collector_run_locator_sha256",
        "official_result_evidence",
        "official_result_evidence_sha256",
        "reset_evidence",
        "reset_evidence_sha256",
        "unit_journal",
        "unit_journal_byte_count",
        "unit_journal_sha256",
    }
    if (
        len(cells) != expected_cell_count
        or set(stage_census) != census_fields
        or any(
            type(stage_census[field]) is not int or cast(int, stage_census[field]) < 0
            for field in stage_census
        )
    ):
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "production pilot cell/stage census differs"
        )
    summed = {field: 0 for field in census_fields}
    switch_roots: list[str] = []
    group_bindings: list[tuple[JsonValue, JsonValue, JsonValue]] = []
    collector_roots: list[str] = []
    collector_run_ids: list[str] = []
    collector_task_run_ids: list[str] = []
    for index, cell_value in enumerate(cells):
        cell = _object(cell_value, code="INVALID_PILOT_STAGE", name="pilot.cell")
        expected_manifest_cell = expected_cells[index]
        expected_fields = base_cell_fields | durable_fields
        if "unit_journal_validated_reference" in cell:
            expected_fields = expected_fields | {"unit_journal_validated_reference"}
        expected_host = "QWEN3_VL" if index % 4 < 2 else "MAI_UI"
        expected_arm = "BASELINE" if index % 2 == 0 else "JOINT_SENTINEL"
        expected_mode = "OFF" if index % 2 == 0 else "ACTIVE"
        decisions = _list(
            cell.get("decisions"), code="INVALID_PILOT_STAGE", name="pilot.cell.decisions"
        )
        census = _object(cell.get("census"), code="INVALID_PILOT_STAGE", name="pilot.cell.census")
        if (
            set(cell) != expected_fields
            or not 1 <= len(decisions) <= expected_pilot_manifest.max_steps_per_cell
            or cell.get("sequence_index") != index
            or cell.get("manifest_sha256") != expected_manifest_sha256
            or cell.get("run_id") != expected_run_id
            or cell.get("host") != expected_host
            or cell.get("arm") != expected_arm
            or cell.get("sentinel_mode") != expected_mode
            or cell.get("task_id") != expected_manifest_cell.task_id
            or cell.get("task_parameters_sha256") != expected_manifest_cell.task_parameters_sha256
            or cell.get("reset_seed") != expected_manifest_cell.reset_seed
            or cell.get("host") != expected_manifest_cell.host.value
            or cell.get("arm") != expected_manifest_cell.arm.value
            or cell.get("sentinel_mode") != expected_manifest_cell.sentinel_mode
            or type(cell.get("actor_resource_sha256")) is not str
            or _SHA256.fullmatch(cast(str, cell["actor_resource_sha256"])) is None
            or cell.get("actor_resource_sha256") != expected_resource_hashes[expected_host]
            or cell.get("history_policy_stage_sha256") != pilot.get("history_policy_stage_sha256")
            or type(cell.get("effective_reset_state_sha256")) is not str
            or _SHA256.fullmatch(cast(str, cell["effective_reset_state_sha256"])) is None
            or not decisions
            or set(census) != census_fields
            or any(
                type(census[field]) is not int or cast(int, census[field]) < 0 for field in census
            )
            or census.get("actor_calls") != len(decisions)
            or census.get("openai_calls")
            != cast(int, census["rubric_openai_calls"])
            + cast(int, census["history_policy_openai_calls"])
            or cast(int, census["actor_actions"]) > cast(int, census["actor_calls"])
        ):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "production pilot cell fields/order differ"
            )
        locator = _object(
            cell.get("collector_run_locator"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.collector_run_locator",
        )
        locator_fields = {
            "collector_manifest_capture_complete",
            "collector_manifest_final_byte_count",
            "collector_manifest_final_path",
            "collector_manifest_final_sha256",
            "collector_manifest_runtime_status",
            "collector_run_id",
            "collector_run_root",
            "collector_task_run_id",
            "manifest_sha256",
            "run_id",
            "sequence_index",
            "task_id",
            "unit_id",
            "unit_journal_sha256",
        }
        reset = _unwrap_production_preimage(
            cell.get("reset_evidence"),
            expected_domain="production-pilot-reset",
            expected_sha256=cell.get("reset_evidence_sha256"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.reset_evidence",
        )
        official_evidence = _unwrap_production_preimage(
            cell.get("official_result_evidence"),
            expected_domain="production-official-result",
            expected_sha256=cell.get("official_result_evidence_sha256"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.official_result_evidence",
        )
        cleanup = _unwrap_production_preimage(
            cell.get("cleanup_evidence"),
            expected_domain="production-unit-cleanup",
            expected_sha256=cell.get("cleanup_evidence_sha256"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.cleanup_evidence",
        )
        official = _object(
            cell.get("official_result"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.official_result",
        )
        journal = _object(
            cell.get("unit_journal"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.unit_journal",
        )
        journal_raw = canonical_json_bytes(cast(JsonValue, journal))
        reset_fields = {
            "backend_endpoint",
            "case_id",
            "effective_reset_state",
            "effective_reset_state_sha256",
            "manifest_sha256",
            "observation_screenshot_sha256",
            "reset_seed",
            "resolved_inputs_sha256",
            "resource_switch_evidence_sha256",
            "task_id",
            "task_name",
            "task_parameters_sha256",
            "trial",
        }
        effective_reset = _object(
            reset.get("effective_reset_state"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.reset_evidence.value.effective_reset_state",
        )
        effective_reset_fields = {
            "observation_screenshot_sha256",
            "reset_seed",
            "task_goal_sha256",
            "task_id",
            "task_name",
            "task_parameters_sha256",
            "trial",
        }
        official_fields = {
            "evaluator_id",
            "official_success_metric_id",
            "official_success_operator",
            "official_success_threshold_float_hex",
            "reason_sha256",
            "result_payload_sha256",
            "score_float_hex",
            "score_ppm",
            "successful",
            "task_id",
        }
        cleanup_fields = {
            "cleanup_dispatch_authorized",
            "collector_manifest_sha256",
            "collector_run_locator",
            "collector_run_locator_sha256",
            "deadline_binding",
            "manifest_sha256",
            "task_run_id",
            "teardown_attempted",
            "teardown_result",
            "teardown_result_sha256",
            "unit_id",
            "unit_journal_sha256",
        }
        teardown = _object(
            cleanup.get("teardown_result"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.cleanup_evidence.value.teardown_result",
        )
        deadline = _require_deadline_projection(
            cleanup.get("deadline_binding"),
            code="INVALID_PILOT_STAGE",
            name="pilot.cell.cleanup_evidence.value.deadline_binding",
        )
        del deadline
        validated_decisions = [
            _require_decision_projection(
                decision,
                index=decision_index,
                arm=expected_arm,
                expected_preflight_report_sha256=expected_preflight_report_sha256,
                expected_factory_binding_sha256=expected_factory_binding_sha256,
            )
            for decision_index, decision in enumerate(decisions, start=1)
        ]
        decision_census = {field: 0 for field in census_fields}
        for decision in validated_decisions:
            current_census = _object(
                decision.get("census"),
                code="INVALID_PILOT_STAGE",
                name="pilot.cell.decision.census",
            )
            for field in census_fields:
                decision_census[field] += cast(int, current_census[field])
        if (
            set(locator) != locator_fields
            or cell.get("collector_run_locator_sha256") != _sha_json(cast(JsonValue, locator))
            or locator.get("manifest_sha256") != expected_manifest_sha256
            or locator.get("run_id") != expected_run_id
            or locator.get("sequence_index") != index
            or locator.get("task_id") != cell.get("task_id")
            or locator.get("unit_id") != f"pilot:{index:03d}"
            or locator.get("collector_manifest_capture_complete") is not True
            or locator.get("collector_manifest_runtime_status") != "completed"
            or type(locator.get("collector_manifest_final_byte_count")) is not int
            or not 1
            <= cast(int, locator["collector_manifest_final_byte_count"])
            <= _MAX_COLLECTOR_MANIFEST_BYTES
            or type(locator.get("collector_manifest_final_sha256")) is not str
            or _SHA256.fullmatch(cast(str, locator["collector_manifest_final_sha256"])) is None
            or type(locator.get("collector_run_id")) is not str
            or _SAFE_ID.fullmatch(cast(str, locator["collector_run_id"])) is None
            or type(locator.get("collector_task_run_id")) is not str
            or _SAFE_ID.fullmatch(cast(str, locator["collector_task_run_id"])) is None
            or type(locator.get("collector_run_root")) is not str
            or not Path(cast(str, locator["collector_run_root"])).is_absolute()
            or str(Path(cast(str, locator["collector_run_root"])))
            != cast(str, locator["collector_run_root"])
            or Path(cast(str, locator["collector_run_root"])).name
            != locator.get("collector_run_id")
            or type(locator.get("collector_manifest_final_path")) is not str
            or Path(cast(str, locator["collector_manifest_final_path"]))
            != Path(cast(str, locator["collector_run_root"])) / "manifest.final.json"
            or cell.get("reset_receipt_sha256") != cell.get("reset_evidence_sha256")
            or cell.get("cleanup_receipt_sha256") != cell.get("cleanup_evidence_sha256")
            or set(reset) != reset_fields
            or set(effective_reset) != effective_reset_fields
            or reset.get("backend_endpoint") != expected_backend_endpoint
            or reset.get("case_id") != f"pilot-cell-{index:03d}"
            or reset.get("resolved_inputs_sha256") != expected_resolved_pilot_inputs_sha256
            or reset.get("trial") != 1
            or reset.get("task_name") != cell.get("task_id")
            or reset.get("observation_screenshot_sha256")
            != effective_reset.get("observation_screenshot_sha256")
            or reset.get("effective_reset_state_sha256")
            != _production_hash(
                "production-pilot-effective-reset-state",
                cast(
                    JsonValue,
                    _pilot_effective_reset_match_projection(effective_reset),
                ),
            )
            or effective_reset.get("reset_seed") != cell.get("reset_seed")
            or effective_reset.get("task_id") != cell.get("task_id")
            or effective_reset.get("task_name") != cell.get("task_id")
            or effective_reset.get("task_parameters_sha256") != cell.get("task_parameters_sha256")
            or effective_reset.get("trial") != 1
            or type(effective_reset.get("task_goal_sha256")) is not str
            or _SHA256.fullmatch(cast(str, effective_reset["task_goal_sha256"])) is None
            or type(effective_reset.get("observation_screenshot_sha256")) is not str
            or _SHA256.fullmatch(cast(str, effective_reset["observation_screenshot_sha256"]))
            is None
            or set(official) != official_fields
            or official.get("task_id") != cell.get("task_id")
            or official.get("evaluator_id") != _OFFICIAL_RESULT_EVALUATOR_ID
            or official.get("official_success_metric_id") != OFFICIAL_SUCCESS_METRIC_ID_V1
            or official.get("official_success_operator") != OFFICIAL_SUCCESS_OPERATOR_V1
            or official.get("official_success_threshold_float_hex")
            != OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1
            or type(official.get("score_float_hex")) is not str
            or _official_score_from_hex(official.get("score_float_hex")) is None
            or type(official.get("score_ppm")) is not int
            or not 0 <= cast(int, official["score_ppm"]) <= 1_000_000
            or round(
                cast(float, _official_score_from_hex(official.get("score_float_hex"))) * 1_000_000
            )
            != official.get("score_ppm")
            or type(official.get("successful")) is not bool
            or official.get("successful")
            is not (
                cast(float, _official_score_from_hex(official.get("score_float_hex")))
                > float.fromhex(OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1)
            )
            or type(official.get("reason_sha256")) is not str
            or _SHA256.fullmatch(cast(str, official["reason_sha256"])) is None
            or cell.get("official_result_evidence_sha256") != official.get("result_payload_sha256")
            or set(official_evidence)
            != {
                "evaluator_id",
                "official_success_metric_id",
                "official_success_operator",
                "official_success_threshold_float_hex",
                "reason",
                "reason_sha256",
                "score_float_hex",
                "score_ppm",
                "task_id",
            }
            or reset.get("manifest_sha256") != expected_manifest_sha256
            or reset.get("task_id") != cell.get("task_id")
            or reset.get("reset_seed") != cell.get("reset_seed")
            or reset.get("task_parameters_sha256") != cell.get("task_parameters_sha256")
            or reset.get("effective_reset_state_sha256") != cell.get("effective_reset_state_sha256")
            or official_evidence.get("task_id") != cell.get("task_id")
            or official_evidence.get("evaluator_id") != official.get("evaluator_id")
            or official_evidence.get("official_success_metric_id")
            != official.get("official_success_metric_id")
            or official_evidence.get("official_success_operator")
            != official.get("official_success_operator")
            or official_evidence.get("official_success_threshold_float_hex")
            != official.get("official_success_threshold_float_hex")
            or official_evidence.get("score_float_hex") != official.get("score_float_hex")
            or official_evidence.get("score_ppm") != official.get("score_ppm")
            or official_evidence.get("reason_sha256") != official.get("reason_sha256")
            or type(official_evidence.get("reason")) is not str
            or hashlib.sha256(cast(str, official_evidence["reason"]).encode("utf-8")).hexdigest()
            != official.get("reason_sha256")
            or set(cleanup) != cleanup_fields
            or cleanup.get("cleanup_dispatch_authorized") is not True
            or cleanup.get("teardown_attempted") is not True
            or cleanup.get("manifest_sha256") != expected_manifest_sha256
            or cleanup.get("unit_id") != f"pilot:{index:03d}"
            or cleanup.get("task_run_id") != locator.get("collector_task_run_id")
            or cleanup.get("collector_manifest_sha256")
            != locator.get("collector_manifest_final_sha256")
            or cleanup.get("collector_run_locator") != locator
            or cleanup.get("collector_run_locator_sha256")
            != cell.get("collector_run_locator_sha256")
            or cleanup.get("unit_journal_sha256") != cell.get("unit_journal_sha256")
            or locator.get("unit_journal_sha256") != cell.get("unit_journal_sha256")
            or cell.get("unit_journal_sha256") != _sha_bytes(journal_raw)
            or cell.get("unit_journal_byte_count") != len(journal_raw)
            or set(teardown)
            != {"message", "message_sha256", "request_dispatched", "status", "task_name"}
            or type(teardown.get("message")) is not str
            or teardown.get("message_sha256")
            != hashlib.sha256(cast(str, teardown["message"]).encode("utf-8")).hexdigest()
            or teardown.get("request_dispatched") is not True
            or teardown.get("status") != "SUCCEEDED"
            or teardown.get("task_name") != cell.get("task_id")
            or cleanup.get("teardown_result_sha256")
            != _production_hash("production-task-teardown-result", cast(JsonValue, teardown))
            or any(
                census[field] != decision_census[field]
                for field in census_fields
                if field != "wall_time_ms"
            )
            or census.get("actor_calls") != len(decisions)
            or cast(int, census["wall_time_ms"]) < decision_census["wall_time_ms"]
            or cast(int, census["wall_time_ms"])
            > expected_pilot_manifest.per_cell_timeout_seconds * 1000
        ):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "production pilot durable cell binding differs"
            )
        is_reference = journal.get("schema_version") == _UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION
        if is_reference != ("unit_journal_validated_reference" in cell):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "production pilot journal storage proof differs"
            )
        if not is_reference and (
            journal.get("unit_id") != f"pilot:{index:03d}"
            or journal.get("reset_evidence") != reset
            or journal.get("reset_evidence_sha256") != cell.get("reset_evidence_sha256")
            or journal.get("official_result_evidence") != official_evidence
            or journal.get("official_result_evidence_sha256")
            != cell.get("official_result_evidence_sha256")
        ):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "inline journal durable bindings differ"
            )
        projection_locator = CollectorRunLocatorV1(
            sequence_index=index,
            manifest_sha256=expected_manifest_sha256,
            run_id=expected_run_id,
            task_id=cast(str, cell["task_id"]),
            host=cast(str, cell["host"]),
            arm=cast(str, cell["arm"]),
            unit_id=f"pilot:{index:03d}",
            run_root=cast(str, locator["collector_run_root"]),
            collector_run_id=cast(str, locator["collector_run_id"]),
            task_run_id=cast(str, locator["collector_task_run_id"]),
            manifest_final_path=cast(str, locator["collector_manifest_final_path"]),
            manifest_final_sha256=cast(str, locator["collector_manifest_final_sha256"]),
            manifest_final_byte_count=cast(int, locator["collector_manifest_final_byte_count"]),
            unit_journal_sha256=cast(str, cell["unit_journal_sha256"]),
            unit_journal=journal,
            unit_journal_validated_reference=(
                None
                if not is_reference
                else _object(
                    cell.get("unit_journal_validated_reference"),
                    code="INVALID_PILOT_STAGE",
                    name="pilot.cell.unit_journal_validated_reference",
                )
            ),
            reset_evidence=reset,
            reset_evidence_sha256=cast(str, cell["reset_evidence_sha256"]),
            official_result=official,
            official_result_evidence=official_evidence,
            official_result_evidence_sha256=cast(str, cell["official_result_evidence_sha256"]),
            cleanup_evidence=cleanup,
            cleanup_evidence_sha256=cast(str, cell["cleanup_evidence_sha256"]),
            decisions=decisions,
            census=census,
            backend_endpoint=expected_backend_endpoint,
            resource_topology=cast(str, expected_run_manifest.resource_topology),
        )
        if not is_reference:
            _validate_full_unit_journal(
                journal, locator=projection_locator, authority=journal_authority
            )
        else:
            reference_preimage = dict(journal)
            reference_sha256 = reference_preimage.pop("reference_sha256", None)
            validation = cast(
                dict[str, JsonValue], projection_locator.unit_journal_validated_reference
            )
            blob = _object(
                journal.get("blob"),
                code="INVALID_PILOT_STAGE",
                name="pilot.cell.unit_journal.blob",
            )
            if (
                set(journal) != {"blob", "reference_sha256", "schema_version", "storage", "unit_id"}
                or journal.get("storage") != _UNIT_JOURNAL_BLOB_STORAGE
                or journal.get("unit_id") != projection_locator.unit_id
                or reference_sha256 != _sha_json(cast(JsonValue, reference_preimage))
                or set(blob) != {"algorithm", "byte_count", "safe_locator", "sha256"}
                or blob.get("algorithm") != "sha256"
                or type(blob.get("sha256")) is not str
                or _SHA256.fullmatch(cast(str, blob["sha256"])) is None
                or blob.get("safe_locator") != f"{blob.get('sha256')}{_UNIT_JOURNAL_BLOB_SUFFIX}"
                or type(blob.get("byte_count")) is not int
                or cast(int, blob["byte_count"]) <= _MAX_INLINE_UNIT_JOURNAL_BYTES
                or set(validation)
                != {
                    "blob",
                    "expected_unit_id",
                    "owner_audit_root_identity_sha256",
                    "reference_preimage_sha256",
                    "reference_sha256",
                    "resource_dispatch_record_count",
                    "resource_dispatch_records_sha256",
                    "schema_version",
                    "validation_status",
                }
                or validation.get("schema_version")
                != _VALIDATED_UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION
                or validation.get("validation_status") != "EXACT_OWNER_ONLY_READBACK"
                or validation.get("expected_unit_id") != projection_locator.unit_id
                or validation.get("reference_preimage_sha256")
                != projection_locator.unit_journal_sha256
                or validation.get("reference_sha256") != reference_sha256
                or validation.get("blob") != blob
                or type(validation.get("resource_dispatch_record_count")) is not int
                or cast(int, validation["resource_dispatch_record_count"]) < 1
            ):
                raise R25PostRunIntegrityError(
                    "INVALID_PILOT_STAGE", "CAS unit journal projection differs"
                )
            for field in (
                "owner_audit_root_identity_sha256",
                "resource_dispatch_records_sha256",
            ):
                _require_sha256_value(
                    validation.get(field),
                    code="INVALID_PILOT_STAGE",
                    name=f"pilot.cell.unit_journal_validated_reference.{field}",
                )
        call_indices = [
            _object(item, code="INVALID_PILOT_STAGE", name="pilot.cell.decision").get(
                "actor_call_index"
            )
            for item in decisions
        ]
        if call_indices != list(range(1, len(decisions) + 1)):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "production pilot actor-call order differs"
            )
        for field in census_fields:
            summed[field] += cast(int, census[field])
        reset_envelope = _object(
            cell.get("reset_evidence"), code="INVALID_PILOT_STAGE", name="reset_evidence"
        )
        reset = _object(
            reset_envelope.get("value"), code="INVALID_PILOT_STAGE", name="reset_evidence.value"
        )
        switch = reset.get("resource_switch_evidence_sha256")
        shared_topology = expected_run_manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
        if shared_topology and index % 2 == 0:
            if type(switch) is not str or _SHA256.fullmatch(switch) is None:
                raise R25PostRunIntegrityError("INVALID_PILOT_STAGE", "pilot switch root is absent")
            switch_roots.append(switch)
        elif switch is not None:
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_STAGE", "pilot reused-host cell repeats a switch"
            )
        group_bindings.append(
            (
                cell.get("task_id"),
                cell.get("task_parameters_sha256"),
                cell.get("effective_reset_state_sha256"),
            )
        )
        collector_roots.append(cast(str, locator["collector_run_root"]))
        collector_run_ids.append(cast(str, locator["collector_run_id"]))
        collector_task_run_ids.append(cast(str, locator["collector_task_run_id"]))
    if (
        stage_census != summed
        or stage_census.get("openai_calls")
        != cast(int, stage_census["rubric_openai_calls"])
        + cast(int, stage_census["history_policy_openai_calls"])
        or cast(int, stage_census["actor_actions"]) > cast(int, stage_census["actor_calls"])
        or cast(int, stage_census["actor_calls"]) > expected_pilot_manifest.max_total_actor_calls
        or cast(int, stage_census["openai_calls"]) > expected_pilot_manifest.max_total_openai_calls
        or cast(int, stage_census["cost_usd_micros"])
        > expected_pilot_manifest.max_total_cost_usd_micros
        or cast(int, stage_census["wall_time_ms"])
        > expected_pilot_manifest.max_total_wall_time_seconds * 1000
        or len(switch_roots)
        != (
            expected_cell_count // 2
            if expected_run_manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            else 0
        )
        or len(set(switch_roots)) != len(switch_roots)
        or len(set(collector_roots)) != expected_cell_count
        or len(set(collector_run_ids)) != expected_cell_count
        or len(set(collector_task_run_ids)) != expected_cell_count
        or any(
            len(set(group_bindings[offset : offset + 4])) != 1
            for offset in range(0, len(group_bindings), 4)
        )
    ):
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_STAGE", "pilot aggregate/switch/matched-cell census differs"
        )
    return _decode_canonical_json(
        canonical_json_bytes(cast(JsonValue, pilot)), code="INVALID_PILOT_STAGE"
    )


def _expected_roots(authority: PostRunIntegrityAuthorityV1) -> dict[str, str]:
    return {
        "factory_binding_sha256": authority.factory_binding_sha256,
        "manifest_sha256": authority.authority_manifest_sha256,
        "preflight_report_sha256": authority.preflight_report_sha256,
        "pricing_sha256": authority.pricing_sha256,
        "runtime_config_sha256": authority.runtime_config_sha256,
        "sentinel_config_sha256": authority.sentinel_config_sha256,
    }


def _require_roots(
    value: dict[str, JsonValue],
    authority: PostRunIntegrityAuthorityV1,
    *,
    code: str,
    name: str,
) -> None:
    for field, expected in _expected_roots(authority).items():
        if value.get(field) != expected:
            raise R25PostRunIntegrityError(code, f"{name}.{field} differs")
    if value.get("run_id") != authority.run_id:
        raise R25PostRunIntegrityError(code, f"{name}.run_id differs")


def _expected_stage_units(stage: str, authority: PostRunIntegrityAuthorityV1) -> list[str]:
    manifest = authority.run_manifest
    if stage == "RESOURCE_PREFLIGHT":
        return ["resources"]
    if stage in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}:
        host = "QWEN3_VL" if stage == "QWEN_LIVE_SMOKE" else "MAI_UI"
        plan = next(item for item in manifest.smoke_plans if item.host.value == host)
        return [f"{host}:{case.mode.value}" for case in plan.cases]
    return [f"pilot-cell-{index:03d}" for index, _ in enumerate(manifest.pilot.cells)]


def _require_stage_receipt(
    receipt: dict[str, JsonValue],
    *,
    stage: str,
    authority: PostRunIntegrityAuthorityV1,
) -> None:
    manifest = authority.run_manifest
    integer_fields = (
        "actor_actions",
        "actor_calls",
        "cost_usd_micros",
        "openai_calls",
        "wall_time_ms",
    )
    if (
        set(receipt) != _STAGE_RECEIPT_FIELDS
        or any(
            type(receipt.get(field)) is not int or cast(int, receipt[field]) < 0
            for field in integer_fields
        )
        or receipt.get("passed") is not True
        or receipt.get("provider_final_request_proven") is not (stage != "RESOURCE_PREFLIGHT")
        or receipt.get("stage") != stage
        or receipt.get("manifest_sha256") != authority.authority_manifest_sha256
        or receipt.get("completed_units") != _expected_stage_units(stage, authority)
        or type(receipt.get("evidence_sha256")) is not str
        or _SHA256.fullmatch(cast(str, receipt["evidence_sha256"])) is None
        or cast(int, receipt["actor_actions"]) > cast(int, receipt["actor_calls"])
    ):
        raise R25PostRunIntegrityError(
            "STAGE_BINDING_MISMATCH", f"{stage} stage receipt fields/census differ"
        )
    if stage == "RESOURCE_PREFLIGHT":
        valid = (
            receipt["actor_calls"] == 0
            and receipt["openai_calls"] == 0
            and receipt["actor_actions"] == 0
            and receipt["cost_usd_micros"] == 0
            and cast(int, receipt["wall_time_ms"])
            <= manifest.max_resource_preflight_wall_time_seconds * 1000
        )
    elif stage in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}:
        host = "QWEN3_VL" if stage == "QWEN_LIVE_SMOKE" else "MAI_UI"
        plan = next(item for item in manifest.smoke_plans if item.host.value == host)
        minimum_openai = sum(0 if case.mode.value == "OFF" else 2 for case in plan.cases)
        maximum_openai = sum(case.max_openai_calls for case in plan.cases)
        handoff_seconds = (
            cast(int, manifest.max_model_switch_wall_time_seconds)
            if stage == "MAI_LIVE_SMOKE"
            and manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            else 0
        )
        valid = (
            receipt["actor_calls"] == sum(case.max_actor_calls for case in plan.cases)
            and minimum_openai <= cast(int, receipt["openai_calls"]) <= maximum_openai
            and receipt["actor_actions"] == 0
            and cast(int, receipt["cost_usd_micros"])
            <= sum(case.max_cost_usd_micros for case in plan.cases)
            and cast(int, receipt["wall_time_ms"])
            <= (sum(case.max_wall_time_seconds for case in plan.cases) + handoff_seconds) * 1000
        )
    else:
        pilot = manifest.pilot
        pilot_switches = (
            cast(int, manifest.max_model_switches) - 1
            if manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            else 0
        )
        valid = (
            len(pilot.cells) <= cast(int, receipt["actor_calls"]) <= pilot.max_total_actor_calls
            and cast(int, receipt["openai_calls"]) <= pilot.max_total_openai_calls
            and cast(int, receipt["cost_usd_micros"]) <= pilot.max_total_cost_usd_micros
            and cast(int, receipt["wall_time_ms"])
            <= (
                pilot.max_total_wall_time_seconds
                + pilot_switches * cast(int, manifest.max_model_switch_wall_time_seconds)
            )
            * 1000
        )
    if not valid:
        raise R25PostRunIntegrityError(
            "STAGE_BINDING_MISMATCH", f"{stage} stage receipt exceeds frozen authority"
        )


def _require_receipt_evidence_census(
    receipt: dict[str, JsonValue],
    census: dict[str, JsonValue],
    *,
    stage: str,
) -> None:
    if any(
        receipt[field] != census[field]
        for field in ("actor_actions", "actor_calls", "cost_usd_micros", "openai_calls")
    ) or cast(int, receipt["wall_time_ms"]) < cast(int, census["wall_time_ms"]):
        raise R25PostRunIntegrityError(
            "STAGE_BINDING_MISMATCH", f"{stage} receipt/evidence census differs"
        )


@dataclass(frozen=True, slots=True)
class _SequenceEvidenceV1:
    output_root: Path
    output_root_identity: dict[str, JsonValue]
    source_commit: str
    binding_raw: bytes
    binding_metadata: os.stat_result
    terminal: dict[str, JsonValue]
    terminal_raw: bytes
    terminal_metadata: os.stat_result
    cleanup: dict[str, JsonValue]
    cleanup_raw: bytes
    cleanup_metadata: os.stat_result
    stages: tuple[tuple[str, str, dict[str, JsonValue], bytes, os.stat_result], ...]
    smoke_evidence: tuple[dict[str, JsonValue], dict[str, JsonValue]]
    pilot_evidence: dict[str, JsonValue]


def _strict_owned_process_projection(
    value: object, *, code: str, name: str
) -> OwnedProcessIdentityV1:
    projection = _object(value, code=code, name=name)
    if set(projection) != {
        "pid",
        "process_group_id",
        "session_id",
        "starttime_ticks",
        "uid",
    }:
        raise R25PostRunIntegrityError(code, f"{name} fields differ")
    try:
        return OwnedProcessIdentityV1(
            pid=cast(int, projection.get("pid")),
            process_group_id=cast(int, projection.get("process_group_id")),
            session_id=cast(int, projection.get("session_id")),
            starttime_ticks=cast(int, projection.get("starttime_ticks")),
            uid=cast(int, projection.get("uid")),
        )
    except (ProductionDriverError, TypeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, f"{name} identity differs") from exc


def _strict_shared_gpu_attestation_projection(
    value: object, *, code: str, name: str
) -> ProductionSharedGpuAttestationV1:
    projection = _object(value, code=code, name=name)
    if set(projection) != {
        "free_memory_mib",
        "gpu_index",
        "gpu_utilization_percent",
        "gpu_uuid",
        "memory_utilization_percent",
        "minimum_free_memory_mib",
        "processes",
        "reserved_memory_mib",
        "total_memory_mib",
        "used_memory_mib",
    }:
        raise R25PostRunIntegrityError(code, f"{name} fields differ")
    processes: list[SharedGpuProcessEvidenceV1] = []
    for offset, process_value in enumerate(
        _list(projection.get("processes"), code=code, name=f"{name}.processes")
    ):
        process = _object(process_value, code=code, name=f"{name}.processes[{offset}]")
        if set(process) != {
            "pid",
            "process_group_id",
            "session_id",
            "starttime_ticks",
            "uid",
            "used_gpu_memory_mib",
            "user",
        }:
            raise R25PostRunIntegrityError(code, f"{name}.process fields differ")
        try:
            processes.append(
                SharedGpuProcessEvidenceV1(
                    pid=cast(int, process.get("pid")),
                    process_group_id=cast(int, process.get("process_group_id")),
                    session_id=cast(int, process.get("session_id")),
                    starttime_ticks=cast(int, process.get("starttime_ticks")),
                    uid=cast(int, process.get("uid")),
                    used_gpu_memory_mib=cast(int, process.get("used_gpu_memory_mib")),
                    user=cast(str, process.get("user")),
                )
            )
        except (ProductionDriverError, TypeError, ValueError) as exc:
            raise R25PostRunIntegrityError(code, f"{name}.process identity differs") from exc
    try:
        attestation = ProductionSharedGpuAttestationV1(
            gpu_index=cast(int, projection.get("gpu_index")),
            gpu_uuid=cast(str, projection.get("gpu_uuid")),
            total_memory_mib=cast(int, projection.get("total_memory_mib")),
            free_memory_mib=cast(int, projection.get("free_memory_mib")),
            used_memory_mib=cast(int, projection.get("used_memory_mib")),
            reserved_memory_mib=cast(int, projection.get("reserved_memory_mib")),
            gpu_utilization_percent=cast(int, projection.get("gpu_utilization_percent")),
            memory_utilization_percent=cast(int, projection.get("memory_utilization_percent")),
            minimum_free_memory_mib=cast(int, projection.get("minimum_free_memory_mib")),
            processes=tuple(processes),
        )
    except (ProductionDriverError, TypeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, f"{name} differs") from exc
    if production_shared_gpu_attestation_projection(attestation) != projection:
        raise R25PostRunIntegrityError(code, f"{name} does not round-trip")
    return attestation


def _strict_model_stop_projection(
    value: object, *, code: str, name: str
) -> ProductionModelStopEvidenceV1:
    projection = _object(value, code=code, name=name)
    if set(projection) != {
        "endpoint",
        "host",
        "leader_reaped",
        "port_available",
        "process",
        "session_members_remaining",
    }:
        raise R25PostRunIntegrityError(code, f"{name} fields differ")
    try:
        stop = ProductionModelStopEvidenceV1(
            host=PilotHostV1(cast(str, projection.get("host"))),
            process=_strict_owned_process_projection(
                projection.get("process"), code=code, name=f"{name}.process"
            ),
            endpoint=cast(str, projection.get("endpoint")),
            leader_reaped=cast(bool, projection.get("leader_reaped")),
            session_members_remaining=cast(int, projection.get("session_members_remaining")),
            port_available=cast(bool, projection.get("port_available")),
        )
    except (ProductionDriverError, TypeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, f"{name} differs") from exc
    if production_model_stop_evidence_projection(stop) != projection:
        raise R25PostRunIntegrityError(code, f"{name} does not round-trip")
    return stop


def validate_production_model_handoff_evidence_projection_v1(
    value: object,
    *,
    expected_manifest_sha256: str,
    expected_runtime_config_sha256: str,
    expected_source_host: PilotHostV1,
    expected_target_host: PilotHostV1,
    expected_gpu_lease_sha256: str | None = None,
) -> ProductionModelHandoffEvidenceV1:
    """Strictly reconstruct one complete production handoff projection."""

    code = "INVALID_MODEL_HANDOFF_EVIDENCE"
    projection = _object(value, code=code, name="model_handoff")
    if set(projection) != {
        "baseline_shared_gpu_attestation",
        "baseline_shared_gpu_attestation_sha256",
        "gpu_lease_sha256",
        "manifest_sha256",
        "post_stop_shared_gpu_attestation",
        "post_stop_shared_gpu_attestation_sha256",
        "resource_topology",
        "runtime_config_sha256",
        "sequence_execution_scope",
        "sequence_scope_authority_sha256",
        "source_host",
        "source_stop",
        "source_stop_sha256",
        "status",
        "target_command_sha256",
        "target_health_sha256",
        "target_host",
        "target_process",
        "target_ready_shared_gpu_attestation",
        "target_ready_shared_gpu_attestation_sha256",
        "target_snapshot_attestation_sha256",
    }:
        raise R25PostRunIntegrityError(code, "model handoff fields differ")
    baseline = _strict_shared_gpu_attestation_projection(
        projection.get("baseline_shared_gpu_attestation"),
        code=code,
        name="model_handoff.baseline_shared_gpu_attestation",
    )
    post_stop = _strict_shared_gpu_attestation_projection(
        projection.get("post_stop_shared_gpu_attestation"),
        code=code,
        name="model_handoff.post_stop_shared_gpu_attestation",
    )
    target_ready = _strict_shared_gpu_attestation_projection(
        projection.get("target_ready_shared_gpu_attestation"),
        code=code,
        name="model_handoff.target_ready_shared_gpu_attestation",
    )
    source_stop = _strict_model_stop_projection(
        projection.get("source_stop"), code=code, name="model_handoff.source_stop"
    )
    target_process = _strict_owned_process_projection(
        projection.get("target_process"),
        code=code,
        name="model_handoff.target_process",
    )
    try:
        handoff = ProductionModelHandoffEvidenceV1(
            manifest_sha256=cast(str, projection.get("manifest_sha256")),
            runtime_config_sha256=cast(str, projection.get("runtime_config_sha256")),
            sequence_execution_scope=cast(str, projection.get("sequence_execution_scope")),
            sequence_scope_authority_sha256=cast(
                str, projection.get("sequence_scope_authority_sha256")
            ),
            resource_topology=ProductionResourceTopologyV1(
                cast(str, projection.get("resource_topology"))
            ),
            gpu_lease_sha256=cast(str, projection.get("gpu_lease_sha256")),
            source_host=PilotHostV1(cast(str, projection.get("source_host"))),
            target_host=PilotHostV1(cast(str, projection.get("target_host"))),
            source_stop=source_stop,
            baseline_shared_gpu_attestation=baseline,
            post_stop_shared_gpu_attestation=post_stop,
            target_command_sha256=cast(str, projection.get("target_command_sha256")),
            target_process=target_process,
            target_health_sha256=cast(str, projection.get("target_health_sha256")),
            target_snapshot_attestation_sha256=cast(
                str, projection.get("target_snapshot_attestation_sha256")
            ),
            target_ready_shared_gpu_attestation=target_ready,
        )
    except (ProductionDriverError, TypeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, "model handoff is invalid") from exc
    if (
        production_model_handoff_evidence_projection(handoff) != projection
        or handoff.manifest_sha256 != expected_manifest_sha256
        or handoff.runtime_config_sha256 != expected_runtime_config_sha256
        or handoff.sequence_scope_authority_sha256 != expected_manifest_sha256
        or handoff.sequence_execution_scope != "R24_R25_FULL"
        or handoff.source_host is not expected_source_host
        or handoff.target_host is not expected_target_host
        or handoff.source_stop.host is not expected_source_host
        or (
            expected_gpu_lease_sha256 is not None
            and handoff.gpu_lease_sha256 != expected_gpu_lease_sha256
        )
        or projection.get("baseline_shared_gpu_attestation_sha256")
        != production_shared_gpu_attestation_sha256(baseline)
        or projection.get("post_stop_shared_gpu_attestation_sha256")
        != production_shared_gpu_attestation_sha256(post_stop)
        or projection.get("target_ready_shared_gpu_attestation_sha256")
        != production_shared_gpu_attestation_sha256(target_ready)
        or projection.get("source_stop_sha256")
        != production_model_stop_evidence_sha256(source_stop)
    ):
        raise R25PostRunIntegrityError(code, "model handoff binding differs")
    return handoff


def _strict_residual_capabilities(value: object, *, code: str) -> dict[str, JsonValue]:
    residual = _object(value, code=code, name="cleanup.residual_capabilities")
    if set(residual) != {
        "admitted_model_processes",
        "backend_candidates",
        "partial_model_processes",
        "pending_backend_ids",
        "pending_backend_names",
    } or any(residual[field] != [] for field in residual):
        raise R25PostRunIntegrityError(code, "cleanup retained a resource capability")
    return residual


def _expected_pilot_switch_authority_sha256(
    authority: PostRunIntegrityAuthorityV1,
) -> str | None:
    manifest = authority.run_manifest
    if manifest.resource_topology == "INDEPENDENT_GPU_CONCURRENT":
        return None
    host_blocks = [
        cell.host.value for index, cell in enumerate(manifest.pilot.cells) if index % 2 == 0
    ]
    return _production_hash(
        "production-pilot-switch-authority",
        cast(
            JsonValue,
            {
                "factory_binding_sha256": authority.factory_binding_sha256,
                "host_blocks": host_blocks,
                "manifest_sha256": authority.authority_manifest_sha256,
                "max_model_switches": manifest.max_model_switches,
                "max_model_switch_wall_time_seconds": manifest.max_model_switch_wall_time_seconds,
                "max_total_model_switch_wall_time_seconds": (
                    manifest.max_total_model_switch_wall_time_seconds
                ),
                "resource_cleanup_upper_bound_seconds": (
                    manifest.max_resource_cleanup_wall_time_seconds
                ),
                "resource_cleanup_upper_bound_sha256": (
                    manifest.resource_cleanup_upper_bound_sha256
                ),
                "runtime_config_sha256": authority.runtime_config_sha256,
                "sequence_scope_authority_sha256": authority.authority_manifest_sha256,
            },
        ),
    )


def _sha_tuple(value: object, *, code: str, name: str) -> tuple[str, ...]:
    items = _list(value, code=code, name=name)
    return tuple(
        _require_sha256_value(item, code=code, name=f"{name}[{index}]")
        for index, item in enumerate(items)
    )


def validate_resource_preflight_stage_evidence_projection_v1(
    evidence: object, *, authority: PostRunIntegrityAuthorityV1
) -> ProductionResourceStageEvidenceV1:
    """Strictly reconstruct the complete production resource-stage preimage."""

    code = "INVALID_RESOURCE_PREFLIGHT_STAGE"
    envelope = _object(evidence, code=code, name="resource_preflight.evidence")
    shared = authority.run_manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
    schema = (
        "mobileworld.runtime.sentinel-r2.4-shared-resource-evidence/v2"
        if shared
        else _PRODUCTION_EVIDENCE_SCHEMA_VERSION
    )
    if (
        set(envelope) != {"domain", "schema_version", "value"}
        or envelope.get("domain") != "production-resource-stage-evidence"
        or envelope.get("schema_version") != schema
    ):
        raise R25PostRunIntegrityError(code, "resource stage envelope differs")
    projection = _object(envelope.get("value"), code=code, name="resource_preflight.evidence.value")
    common = {
        "backend_command_sha256",
        "backend_container_id",
        "backend_health_sha256",
        "gpu_idle_attestation_sha256s",
        "gpu_lease_sha256s",
        "manifest_sha256",
        "model_command_sha256s",
        "model_health_sha256s",
        "model_processes",
        "runtime_attestation_sha256",
        "runtime_config_sha256",
    }
    shared_only = {
        "active_hosts",
        "minimum_free_gpu_memory_mib",
        "pilot_switch_authority_sha256",
        "resource_topology",
        "sequence_execution_scope",
        "sequence_scope_authority_sha256",
        "shared_gpu_attestation_sha256s",
        "shared_gpu_attestations",
        "vllm_gpu_memory_utilization",
    }
    if set(projection) != (common | shared_only if shared else common):
        raise R25PostRunIntegrityError(code, "resource stage fields differ")
    processes = tuple(
        _strict_owned_process_projection(item, code=code, name=f"resource.model_processes[{index}]")
        for index, item in enumerate(
            _list(
                projection.get("model_processes"),
                code=code,
                name="resource.model_processes",
            )
        )
    )
    attestation_values = (
        _list(
            projection.get("shared_gpu_attestations"),
            code=code,
            name="resource.shared_gpu_attestations",
        )
        if shared
        else []
    )
    attestations = tuple(
        _strict_shared_gpu_attestation_projection(
            item, code=code, name=f"resource.shared_gpu_attestations[{index}]"
        )
        for index, item in enumerate(attestation_values)
    )
    if shared and _sha_tuple(
        projection.get("shared_gpu_attestation_sha256s"),
        code=code,
        name="resource.shared_gpu_attestation_sha256s",
    ) != tuple(production_shared_gpu_attestation_sha256(item) for item in attestations):
        raise R25PostRunIntegrityError(code, "resource GPU attestation roots differ")
    try:
        typed = ProductionResourceStageEvidenceV1(
            manifest_sha256=cast(str, projection.get("manifest_sha256")),
            runtime_config_sha256=cast(str, projection.get("runtime_config_sha256")),
            runtime_attestation_sha256=cast(str, projection.get("runtime_attestation_sha256")),
            backend_command_sha256=cast(str, projection.get("backend_command_sha256")),
            backend_container_id=cast(str, projection.get("backend_container_id")),
            backend_health_sha256=cast(str, projection.get("backend_health_sha256")),
            model_command_sha256s=cast(
                tuple[str, str],
                _sha_tuple(
                    projection.get("model_command_sha256s"),
                    code=code,
                    name="resource.model_command_sha256s",
                ),
            ),
            model_processes=processes,
            model_health_sha256s=_sha_tuple(
                projection.get("model_health_sha256s"),
                code=code,
                name="resource.model_health_sha256s",
            ),
            gpu_lease_sha256s=_sha_tuple(
                projection.get("gpu_lease_sha256s"),
                code=code,
                name="resource.gpu_lease_sha256s",
            ),
            gpu_idle_attestation_sha256s=_sha_tuple(
                projection.get("gpu_idle_attestation_sha256s"),
                code=code,
                name="resource.gpu_idle_attestation_sha256s",
            ),
            shared_gpu_attestations=attestations,
            active_hosts=(
                tuple(
                    PilotHostV1(cast(str, item))
                    for item in _list(
                        projection.get("active_hosts"),
                        code=code,
                        name="resource.active_hosts",
                    )
                )
                if shared
                else (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI)
            ),
            resource_topology=(
                ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                if shared
                else ProductionResourceTopologyV1.INDEPENDENT_GPU_CONCURRENT
            ),
            vllm_gpu_memory_utilization=(
                cast(str, projection.get("vllm_gpu_memory_utilization")) if shared else "0.90"
            ),
            minimum_free_gpu_memory_mib=(
                cast(int, projection.get("minimum_free_gpu_memory_mib")) if shared else 0
            ),
            sequence_execution_scope="R24_R25_FULL",
            sequence_scope_authority_sha256=authority.authority_manifest_sha256,
            pilot_switch_authority_sha256=(
                cast(str, projection.get("pilot_switch_authority_sha256")) if shared else None
            ),
        )
    except (ProductionDriverError, TypeError, ValueError) as exc:
        raise R25PostRunIntegrityError(code, "resource stage is invalid") from exc
    if (
        production_resource_stage_evidence_projection(typed) != projection
        or typed.manifest_sha256 != authority.authority_manifest_sha256
        or typed.runtime_config_sha256 != authority.runtime_config_sha256
        or typed.sequence_scope_authority_sha256 != authority.authority_manifest_sha256
        or (
            shared
            and typed.pilot_switch_authority_sha256
            != _expected_pilot_switch_authority_sha256(authority)
        )
        or production_resource_stage_evidence_sha256(typed) != _sha_json(cast(JsonValue, envelope))
    ):
        raise R25PostRunIntegrityError(code, "resource stage binding differs")
    return typed


def _validate_smoke_inline_unit_journal(
    journal: dict[str, JsonValue],
    *,
    expected_unit_id: str,
    decision: dict[str, JsonValue],
    authority: PostRunIntegrityAuthorityV1,
) -> None:
    code = "INVALID_SMOKE_STAGE"
    if set(journal) != {
        "cleanup_recovery_outcome",
        "cleanup_recovery_outcome_sha256",
        "collector_run_binding",
        "collector_run_binding_sha256",
        "completed_decisions",
        "completed_decisions_sha256",
        "official_result_evidence",
        "official_result_evidence_sha256",
        "reset_evidence",
        "reset_evidence_sha256",
        "resource_dispatch_records",
        "resource_dispatch_records_sha256",
        "run_fatal_state",
        "run_fatal_state_sha256",
        "terminal_audit_records",
        "terminal_audit_records_sha256",
        "unit_deadline",
        "unit_id",
    }:
        raise R25PostRunIntegrityError(code, "smoke journal fields differ")
    decisions = _require_projection_list_hash(
        journal,
        field="completed_decisions",
        domain="production-unit-decision-journal",
    )
    terminals = _require_projection_list_hash(
        journal,
        field="terminal_audit_records",
        domain="production-unit-terminal-audit-journal",
    )
    dispatches = _require_projection_list_hash(
        journal,
        field="resource_dispatch_records",
        domain="production-unit-resource-dispatch-journal",
    )
    cleanup = _object(
        journal.get("cleanup_recovery_outcome"),
        code=code,
        name="smoke.journal.cleanup_recovery_outcome",
    )
    collector_binding = _object(
        journal.get("collector_run_binding"),
        code=code,
        name="smoke.journal.collector_run_binding",
    )
    if set(collector_binding) != {
        "collector_manifest_capture_complete",
        "collector_manifest_final_byte_count",
        "collector_manifest_final_path",
        "collector_manifest_final_sha256",
        "collector_manifest_runtime_status",
        "collector_run_id",
        "collector_run_root",
        "collector_task_run_id",
    }:
        raise R25PostRunIntegrityError(code, "smoke Collector binding fields differ")
    if (
        journal.get("unit_id") != expected_unit_id
        or decisions != [decision]
        or not dispatches
        or journal.get("run_fatal_state") is not None
        or journal.get("run_fatal_state_sha256") is not None
        or set(cleanup)
        != {
            "initialization_permitted",
            "message_sha256",
            "outcome",
            "request_dispatched",
            "teardown_attempted",
        }
        or cleanup.get("initialization_permitted") is not False
        or cleanup.get("outcome") != "SUCCEEDED"
        or cleanup.get("request_dispatched") is not True
        or cleanup.get("teardown_attempted") is not True
        or journal.get("cleanup_recovery_outcome_sha256")
        != _production_hash("production-unit-cleanup-recovery-outcome", cast(JsonValue, cleanup))
        or journal.get("collector_run_binding_sha256")
        != _production_hash("production-collector-run-binding", cast(JsonValue, collector_binding))
        or collector_binding.get("collector_manifest_capture_complete") is not True
        or collector_binding.get("collector_manifest_runtime_status") != "completed"
        or journal.get("official_result_evidence") is not None
        or journal.get("official_result_evidence_sha256") is not None
        or journal.get("reset_evidence") is not None
        or journal.get("reset_evidence_sha256") is not None
    ):
        raise R25PostRunIntegrityError(code, "smoke journal binding differs")
    _require_sha256_value(
        cleanup.get("message_sha256"), code=code, name="smoke.cleanup.message_sha256"
    )
    _require_deadline_projection(
        journal.get("unit_deadline"), code=code, name="smoke.journal.unit_deadline"
    )
    _validate_terminal_audit_records(
        terminals,
        decisions=[cast(JsonValue, decision)],
        authority=authority,
    )


def validate_smoke_stage_durable_evidence_projection_v2(
    smoke: dict[str, JsonValue],
    *,
    authority: PostRunIntegrityAuthorityV1,
    expected_stage: str,
) -> dict[str, JsonValue]:
    """Validate one exact Qwen/MAI production smoke stage projection."""

    code = "INVALID_SMOKE_STAGE"
    expected_host = "QWEN3_VL" if expected_stage == "QWEN_LIVE_SMOKE" else "MAI_UI"
    if expected_stage not in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}:
        raise R25PostRunIntegrityError(code, "smoke stage identity differs")
    expected_schema = _FULL_SMOKE_EVIDENCE_SCHEMA_VERSION
    if set(smoke) != {
        "actor_resource_sha256",
        "cases",
        "census",
        "history_policy_stage_sha256",
        "host",
        "manifest_sha256",
        "run_id",
        "schema_version",
        "stage",
    }:
        raise R25PostRunIntegrityError(code, "smoke stage fields differ")
    manifest_projection = authority_manifest_projection(authority.run_manifest)
    resources = cast(list[dict[str, JsonValue]], manifest_projection["actor_resources"])
    resource = next((item for item in resources if item.get("host") == expected_host), None)
    policy_stage = next(
        (
            cast(dict[str, JsonValue], item)
            for item in cast(list[JsonValue], manifest_projection["openai_stages"])
            if cast(dict[str, JsonValue], item).get("role") == "HISTORY_POLICY"
        ),
        None,
    )
    expected_resource_sha256 = (
        None if resource is None else _production_hash("actor-resource", cast(JsonValue, resource))
    )
    expected_policy_sha256 = (
        None
        if policy_stage is None
        else _production_hash("history-policy-stage", cast(JsonValue, policy_stage))
    )
    plan = next(
        (item for item in authority.run_manifest.smoke_plans if item.host.value == expected_host),
        None,
    )
    if (
        smoke.get("schema_version") != expected_schema
        or smoke.get("manifest_sha256") != authority.authority_manifest_sha256
        or smoke.get("run_id") != authority.run_id
        or smoke.get("stage") != expected_stage
        or smoke.get("host") != expected_host
        or smoke.get("actor_resource_sha256") != expected_resource_sha256
        or smoke.get("history_policy_stage_sha256") != expected_policy_sha256
        or plan is None
    ):
        raise R25PostRunIntegrityError(code, "smoke authority binding differs")
    cases = _list(smoke.get("cases"), code=code, name="smoke.cases")
    stage_census = _object(smoke.get("census"), code=code, name="smoke.census")
    if (
        len(cases) != 3
        or set(stage_census) != _CENSUS_FIELDS
        or any(
            type(stage_census[field]) is not int or cast(int, stage_census[field]) < 0
            for field in stage_census
        )
        or stage_census.get("openai_calls")
        != cast(int, stage_census["rubric_openai_calls"])
        + cast(int, stage_census["history_policy_openai_calls"])
        or cast(int, stage_census["actor_actions"]) > cast(int, stage_census["actor_calls"])
    ):
        raise R25PostRunIntegrityError(code, "smoke case census differs")
    summed = {field: 0 for field in _CENSUS_FIELDS}
    for index, (case_value, expected_case) in enumerate(zip(cases, plan.cases, strict=True)):
        case = _object(case_value, code=code, name=f"smoke.cases[{index}]")
        base_fields = {
            "actor_resource_sha256",
            "case_id",
            "census",
            "cleanup_receipt_sha256",
            "decision",
            "history_policy_stage_sha256",
            "host",
            "manifest_sha256",
            "mode",
            "request_fixture_byte_count",
            "request_fixture_sha256",
            "run_id",
            "sequence_index",
            "stage",
            "task_id",
        }
        expected_fields = base_fields
        if expected_schema == _FULL_SMOKE_EVIDENCE_SCHEMA_VERSION:
            expected_fields |= {
                "unit_journal",
                "unit_journal_byte_count",
                "unit_journal_sha256",
            }
            if "unit_journal_validated_reference" in case:
                expected_fields |= {"unit_journal_validated_reference"}
        decision = _require_decision_projection(
            case.get("decision"),
            index=1,
            arm=("BASELINE" if expected_case.mode.value == "OFF" else "JOINT_SENTINEL"),
            expected_preflight_report_sha256=authority.preflight_report_sha256,
            expected_factory_binding_sha256=authority.factory_binding_sha256,
            allow_first_call_history_policy=True,
        )
        census = _require_census_projection(
            case.get("census"),
            code=code,
            name=f"smoke.cases[{index}].census",
            require_one_actor_call=True,
        )
        decision_census = _object(decision.get("census"), code=code, name="smoke.decision.census")
        if (
            set(case) != expected_fields
            or case.get("manifest_sha256") != authority.authority_manifest_sha256
            or case.get("run_id") != authority.run_id
            or case.get("stage") != expected_stage
            or case.get("host") != expected_host
            or case.get("sequence_index") != index
            or case.get("case_id") != expected_case.case_id
            or case.get("task_id") != expected_case.task_id
            or case.get("mode") != expected_case.mode.value
            or case.get("actor_resource_sha256") != expected_resource_sha256
            or case.get("history_policy_stage_sha256") != expected_policy_sha256
            or case.get("request_fixture_sha256") != expected_case.request_fixture_sha256
            or case.get("request_fixture_byte_count") != expected_case.request_fixture_byte_count
            or census.get("actor_actions") != 0
            or decision.get("executed_action_sha256") is not None
            or cast(int, census["openai_calls"]) > expected_case.max_openai_calls
            or cast(int, census["cost_usd_micros"]) > expected_case.max_cost_usd_micros
            or cast(int, census["wall_time_ms"]) > expected_case.max_wall_time_seconds * 1000
            or any(
                census[field] != decision_census[field]
                for field in _CENSUS_FIELDS
                if field != "wall_time_ms"
            )
            or cast(int, census["wall_time_ms"]) < cast(int, decision_census["wall_time_ms"])
            or (
                expected_case.mode.value in {"OFF", "SHADOW"}
                and decision.get("final_request_sha256") != decision.get("raw_request_sha256")
            )
        ):
            raise R25PostRunIntegrityError(code, "smoke case binding differs")
        _require_sha256_value(
            case.get("cleanup_receipt_sha256"),
            code=code,
            name="smoke.cleanup_receipt_sha256",
        )
        if expected_schema == _FULL_SMOKE_EVIDENCE_SCHEMA_VERSION:
            journal = _object(case.get("unit_journal"), code=code, name="smoke.unit_journal")
            journal_raw = canonical_json_bytes(cast(JsonValue, journal))
            if (
                case.get("unit_journal_sha256") != _sha_bytes(journal_raw)
                or case.get("unit_journal_byte_count") != len(journal_raw)
                or journal.get("schema_version") == _UNIT_JOURNAL_REFERENCE_SCHEMA_VERSION
            ):
                raise R25PostRunIntegrityError(code, "smoke journal requires inline exact evidence")
            _validate_smoke_inline_unit_journal(
                journal,
                expected_unit_id=f"smoke:{expected_host}:{expected_case.mode.value}",
                decision=decision,
                authority=authority,
            )
        for field in _CENSUS_FIELDS:
            summed[field] += cast(int, census[field])
    if stage_census != summed:
        raise R25PostRunIntegrityError(code, "smoke stage census differs")
    return _decode_canonical_json(canonical_json_bytes(cast(JsonValue, smoke)), code=code)


def smoke_collector_run_locators_from_stage_evidence_v2(
    smoke_evidence: tuple[dict[str, JsonValue], dict[str, JsonValue]],
    *,
    authority: PostRunIntegrityAuthorityV1,
) -> tuple[SmokeCollectorRunLocatorV1, ...]:
    """Rebuild the exact six Qwen-then-MAI smoke Collector locators."""

    if type(smoke_evidence) is not tuple or len(smoke_evidence) != 2:
        raise R25PostRunIntegrityError(
            "INVALID_SMOKE_COLLECTOR_CENSUS", "two smoke stage preimages are required"
        )
    trusted_stages = (
        validate_smoke_stage_durable_evidence_projection_v2(
            smoke_evidence[0], authority=authority, expected_stage="QWEN_LIVE_SMOKE"
        ),
        validate_smoke_stage_durable_evidence_projection_v2(
            smoke_evidence[1], authority=authority, expected_stage="MAI_LIVE_SMOKE"
        ),
    )
    locators: list[SmokeCollectorRunLocatorV1] = []
    seen_roots: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_task_run_ids: set[str] = set()
    for stage_index, stage in enumerate(trusted_stages):
        cases = _list(stage.get("cases"), code="INVALID_SMOKE_STAGE", name="smoke.cases")
        for local_index, case_value in enumerate(cases):
            case = _object(case_value, code="INVALID_SMOKE_STAGE", name="smoke.case")
            journal = _object(
                case.get("unit_journal"), code="INVALID_SMOKE_STAGE", name="smoke.unit_journal"
            )
            binding = _object(
                journal.get("collector_run_binding"),
                code="INVALID_SMOKE_COLLECTOR_LOCATOR",
                name="smoke.collector_run_binding",
            )
            global_index = stage_index * 3 + local_index
            expected_host = "QWEN3_VL" if stage_index == 0 else "MAI_UI"
            expected_stage = "QWEN_LIVE_SMOKE" if stage_index == 0 else "MAI_LIVE_SMOKE"
            unit_id = f"smoke:{expected_host}:{case.get('mode')}"
            unit_journal_sha256 = case.get("unit_journal_sha256")
            if (
                case.get("sequence_index") != local_index
                or case.get("stage") != expected_stage
                or case.get("host") != expected_host
                or journal.get("unit_id") != unit_id
                or type(unit_journal_sha256) is not str
                or _SHA256.fullmatch(unit_journal_sha256) is None
                or unit_journal_sha256 != _sha_json(cast(JsonValue, journal))
            ):
                raise R25PostRunIntegrityError(
                    "INVALID_SMOKE_COLLECTOR_LOCATOR", "smoke locator identity differs"
                )
            trusted = SmokeCollectorRunLocatorV1(
                sequence_index=global_index,
                manifest_sha256=authority.authority_manifest_sha256,
                run_id=authority.run_id,
                stage=expected_stage,
                host=expected_host,
                mode=cast(str, case["mode"]),
                case_id=cast(str, case["case_id"]),
                task_id=cast(str, case["task_id"]),
                unit_id=unit_id,
                run_root=cast(str, binding["collector_run_root"]),
                collector_run_id=cast(str, binding["collector_run_id"]),
                task_run_id=cast(str, binding["collector_task_run_id"]),
                manifest_final_path=cast(str, binding["collector_manifest_final_path"]),
                manifest_final_sha256=cast(str, binding["collector_manifest_final_sha256"]),
                manifest_final_byte_count=cast(int, binding["collector_manifest_final_byte_count"]),
                unit_journal_sha256=unit_journal_sha256,
                unit_journal=journal,
                decision=_object(
                    case.get("decision"), code="INVALID_SMOKE_STAGE", name="smoke.decision"
                ),
                census=_object(case.get("census"), code="INVALID_SMOKE_STAGE", name="smoke.census"),
            )
            for value, seen, name in (
                (trusted.run_root, seen_roots, "run root"),
                (trusted.collector_run_id, seen_run_ids, "run ID"),
                (trusted.task_run_id, seen_task_run_ids, "task run ID"),
            ):
                if value in seen:
                    raise R25PostRunIntegrityError(
                        "DUPLICATE_COLLECTOR_RUN", f"smoke Collector {name} is not unique"
                    )
                seen.add(value)
            locators.append(trusted)
    if len(locators) != 6 or [item.sequence_index for item in locators] != list(range(6)):
        raise R25PostRunIntegrityError(
            "INVALID_SMOKE_COLLECTOR_CENSUS", "smoke Collector order/count differs"
        )
    return tuple(locators)


def _require_pilot_switch_cleanup_bridge(
    *,
    pilot_evidence: dict[str, JsonValue],
    cleanup_evidence: dict[str, JsonValue],
    authority: PostRunIntegrityAuthorityV1,
    resource_evidence: ProductionResourceStageEvidenceV1,
    initial_handoff: ProductionModelHandoffEvidenceV1 | None,
) -> None:
    code = "RESOURCE_CLEANUP_NOT_PROVEN"
    if (
        set(cleanup_evidence) != {"domain", "schema_version", "value"}
        or cleanup_evidence.get("domain") != "production-resource-cleanup-evidence"
        or cleanup_evidence.get("schema_version")
        != "mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"
    ):
        raise R25PostRunIntegrityError(code, "cleanup envelope differs")
    cleanup_value = _object(
        cleanup_evidence.get("value"), code=code, name="resource_cleanup_evidence.value"
    )
    manifest = authority.run_manifest
    common_fields = {
        "backend_container_id",
        "gpu_lease_released",
        "gpu_lease_sha256s",
        "manifest_sha256",
        "reclaimed_cleanup_outcome",
        "reclaimed_cleanup_outcome_sha256",
        "residual_capabilities",
        "residual_capabilities_sha256",
        "resource_topology",
        "runtime_config_sha256",
        "sequence_execution_scope",
        "sequence_scope_authority_sha256",
        "status",
        "stopped_model_sha256s",
        "stopped_models",
    }
    shared_fields = common_fields | {
        "baseline_shared_gpu_attestation",
        "cleanup_outcome",
        "final_shared_gpu_attestation",
        "final_shared_gpu_attestation_sha256",
        "minimum_free_gpu_memory_mib",
        "pilot_model_switch_evidence",
        "pilot_model_switch_evidence_sha256s",
        "shared_gpu_tenant_continuity_status",
        "unconsumed_pilot_model_switch_evidence_sha256s",
        "vllm_gpu_memory_utilization",
    }
    topology = manifest.resource_topology
    expected_fields = shared_fields if topology == "SINGLE_GPU_SEQUENTIAL_SHARED" else common_fields
    residual = _strict_residual_capabilities(cleanup_value.get("residual_capabilities"), code=code)
    lease_roots = _list(
        cleanup_value.get("gpu_lease_sha256s"),
        code=code,
        name="cleanup.gpu_lease_sha256s",
    )
    for index, lease_root in enumerate(lease_roots):
        _require_sha256_value(lease_root, code=code, name=f"cleanup.gpu_lease_sha256s[{index}]")
    stopped_values = _list(
        cleanup_value.get("stopped_models"), code=code, name="cleanup.stopped_models"
    )
    stopped_roots = _list(
        cleanup_value.get("stopped_model_sha256s"),
        code=code,
        name="cleanup.stopped_model_sha256s",
    )
    stopped_models = [
        _strict_model_stop_projection(value, code=code, name=f"cleanup.stopped_models[{index}]")
        for index, value in enumerate(stopped_values)
    ]
    expected_stopped_roots = [
        production_model_stop_evidence_sha256(value) for value in stopped_models
    ]
    reclaimed = _object(
        cleanup_value.get("reclaimed_cleanup_outcome"),
        code=code,
        name="cleanup.reclaimed_cleanup_outcome",
    )
    if (
        set(reclaimed) != {"domain", "schema_version", "value"}
        or reclaimed.get("domain") != "production-resource-reclaimed-cleanup-outcome"
        or reclaimed.get("schema_version")
        != "mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"
        or cleanup_value.get("reclaimed_cleanup_outcome_sha256")
        != _sha_json(cast(JsonValue, reclaimed))
    ):
        raise R25PostRunIntegrityError(code, "reclaimed cleanup envelope differs")
    reclaimed_value = _object(
        reclaimed.get("value"), code=code, name="cleanup.reclaimed_cleanup_outcome.value"
    )
    reclaimed_fields = {
        "backend_container_id",
        "final_shared_gpu_attestation",
        "gpu_lease_sha256s",
        "manifest_sha256",
        "residual_capabilities",
        "resource_topology",
        "runtime_config_sha256",
        "sequence_execution_scope",
        "sequence_scope_authority_sha256",
        "status",
        "stopped_models",
    }
    cells = _list(pilot_evidence.get("cells"), code=code, name="pilot_evidence.cells")
    reset_switch_roots: list[JsonValue] = []
    for index in range(0, len(cells), 2):
        cell = _object(cells[index], code=code, name="pilot_evidence.cell")
        reset = _unwrap_production_preimage(
            cell.get("reset_evidence"),
            expected_domain="production-pilot-reset",
            expected_sha256=cell.get("reset_evidence_sha256"),
            code=code,
            name="pilot_evidence.cell.reset_evidence",
        )
        reset_switch_roots.append(reset.get("resource_switch_evidence_sha256"))
    expected_host_blocks = [
        cell.host.value for index, cell in enumerate(manifest.pilot.cells) if index % 2 == 0
    ]
    switch_authority_projection: dict[str, JsonValue] = {
        "factory_binding_sha256": authority.factory_binding_sha256,
        "host_blocks": cast(JsonValue, expected_host_blocks),
        "manifest_sha256": authority.authority_manifest_sha256,
        "max_model_switches": cast(int, manifest.max_model_switches),
        "max_model_switch_wall_time_seconds": cast(
            int, manifest.max_model_switch_wall_time_seconds
        ),
        "max_total_model_switch_wall_time_seconds": cast(
            int, manifest.max_total_model_switch_wall_time_seconds
        ),
        "resource_cleanup_upper_bound_seconds": cast(
            int, manifest.max_resource_cleanup_wall_time_seconds
        ),
        "resource_cleanup_upper_bound_sha256": cast(
            str, manifest.resource_cleanup_upper_bound_sha256
        ),
        "runtime_config_sha256": authority.runtime_config_sha256,
        "sequence_scope_authority_sha256": authority.authority_manifest_sha256,
    }
    expected_switch_authority = _production_hash(
        "production-pilot-switch-authority",
        cast(JsonValue, switch_authority_projection),
    )
    if (
        set(cleanup_value) != expected_fields
        or set(reclaimed_value) != reclaimed_fields
        or cleanup_value.get("manifest_sha256") != authority.authority_manifest_sha256
        or cleanup_value.get("runtime_config_sha256") != authority.runtime_config_sha256
        or cleanup_value.get("resource_topology") != topology
        or cleanup_value.get("sequence_scope_authority_sha256")
        != authority.authority_manifest_sha256
        or cleanup_value.get("sequence_execution_scope") != "R24_R25_FULL"
        or cleanup_value.get("status") != "CLEANED"
        or cleanup_value.get("gpu_lease_released") is not True
        or cleanup_value.get("backend_container_id") != resource_evidence.backend_container_id
        or tuple(cast(list[str], lease_roots)) != resource_evidence.gpu_lease_sha256s
        or not lease_roots
        or len(lease_roots) != len(set(cast(list[str], lease_roots)))
        or not stopped_models
        or stopped_roots != cast(list[JsonValue], expected_stopped_roots)
        or cleanup_value.get("residual_capabilities_sha256")
        != _production_hash("production-resource-residual-capabilities", cast(JsonValue, residual))
        or reclaimed_value.get("backend_container_id") != cleanup_value.get("backend_container_id")
        or reclaimed_value.get("gpu_lease_sha256s") != lease_roots
        or reclaimed_value.get("manifest_sha256") != authority.authority_manifest_sha256
        or reclaimed_value.get("residual_capabilities") != residual
        or reclaimed_value.get("resource_topology") != topology
        or reclaimed_value.get("runtime_config_sha256") != authority.runtime_config_sha256
        or reclaimed_value.get("sequence_execution_scope") != "R24_R25_FULL"
        or reclaimed_value.get("sequence_scope_authority_sha256")
        != authority.authority_manifest_sha256
        or reclaimed_value.get("status") != "RECLAIMED"
        or reclaimed_value.get("stopped_models") != stopped_values
    ):
        raise R25PostRunIntegrityError(code, "complete cleanup projection differs")
    if topology == "INDEPENDENT_GPU_CONCURRENT":
        if (
            any(root is not None for root in reset_switch_roots)
            or len(lease_roots) != 2
            or reclaimed_value.get("final_shared_gpu_attestation") is not None
            or initial_handoff is not None
        ):
            raise R25PostRunIntegrityError(code, "concurrent cleanup topology differs")
        return
    if topology != "SINGLE_GPU_SEQUENTIAL_SHARED":
        raise R25PostRunIntegrityError(code, "cleanup topology is outside the closed set")
    switch_evidence = _list(
        cleanup_value.get("pilot_model_switch_evidence"),
        code=code,
        name="cleanup.pilot_model_switch_evidence",
    )
    switch_roots = _list(
        cleanup_value.get("pilot_model_switch_evidence_sha256s"),
        code=code,
        name="cleanup.pilot_model_switch_evidence_sha256s",
    )
    baseline = _strict_shared_gpu_attestation_projection(
        cleanup_value.get("baseline_shared_gpu_attestation"),
        code=code,
        name="cleanup.baseline_shared_gpu_attestation",
    )
    final_attestation = _strict_shared_gpu_attestation_projection(
        cleanup_value.get("final_shared_gpu_attestation"),
        code=code,
        name="cleanup.final_shared_gpu_attestation",
    )
    if (
        cleanup_value.get("cleanup_outcome") != "SHARED_MODELS_RECLAIMED"
        or cleanup_value.get("shared_gpu_tenant_continuity_status") != "NOT_INSPECTED"
        or cleanup_value.get("minimum_free_gpu_memory_mib") != 51_200
        or cleanup_value.get("vllm_gpu_memory_utilization") != "0.24"
        or cleanup_value.get("final_shared_gpu_attestation_sha256")
        != production_shared_gpu_attestation_sha256(final_attestation)
        or baseline.gpu_index != final_attestation.gpu_index
        or not resource_evidence.shared_gpu_attestations
        or baseline != resource_evidence.shared_gpu_attestations[0]
        or baseline.gpu_uuid != final_attestation.gpu_uuid
        or baseline.minimum_free_memory_mib != 51_200
        or baseline.free_memory_mib < baseline.minimum_free_memory_mib
        or final_attestation.minimum_free_memory_mib != 0
        or baseline.processes
        or final_attestation.processes
        or reclaimed_value.get("final_shared_gpu_attestation")
        != cleanup_value.get("final_shared_gpu_attestation")
        or len(lease_roots) != 1
        or cleanup_value.get("unconsumed_pilot_model_switch_evidence_sha256s") != []
        or len(switch_evidence) != len(expected_host_blocks)
        or switch_roots != reset_switch_roots
        or initial_handoff is None
        or initial_handoff.baseline_shared_gpu_attestation != baseline
        or production_model_stop_evidence_sha256(initial_handoff.source_stop)
        not in expected_stopped_roots
    ):
        raise R25PostRunIntegrityError(code, "shared cleanup/reset switch census differs")
    transition_stop_roots: list[str] = []
    for index, (raw_switch, digest) in enumerate(zip(switch_evidence, switch_roots, strict=True)):
        switch = _object(raw_switch, code=code, name="cleanup.pilot_model_switch_evidence")
        value = _object(switch.get("value"), code=code, name="pilot_model_switch.value")
        expected_target = PilotHostV1(expected_host_blocks[index])
        expected_source = (
            PilotHostV1.MAI_UI if expected_target is PilotHostV1.QWEN3_VL else PilotHostV1.QWEN3_VL
        )
        handoff = validate_production_model_handoff_evidence_projection_v1(
            value.get("transition"),
            expected_manifest_sha256=authority.authority_manifest_sha256,
            expected_runtime_config_sha256=authority.runtime_config_sha256,
            expected_source_host=expected_source,
            expected_target_host=expected_target,
            expected_gpu_lease_sha256=cast(str, lease_roots[0]),
        )
        try:
            typed_switch = ProductionPilotModelSwitchEvidenceV1(
                switch_authority_sha256=expected_switch_authority,
                switch_index=index,
                pilot_host_block_count=len(expected_host_blocks),
                max_model_switches=cast(int, manifest.max_model_switches),
                max_model_switch_wall_time_seconds=cast(
                    int, manifest.max_model_switch_wall_time_seconds
                ),
                max_total_model_switch_wall_time_seconds=cast(
                    int, manifest.max_total_model_switch_wall_time_seconds
                ),
                transition=handoff,
            )
        except (ProductionDriverError, TypeError, ValueError) as exc:
            raise R25PostRunIntegrityError(code, "pilot switch is invalid") from exc
        if (
            type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or digest != production_pilot_model_switch_evidence_sha256(typed_switch)
            or set(switch) != {"domain", "schema_version", "value"}
            or switch.get("domain") != "production-pilot-model-switch-evidence"
            or switch.get("schema_version")
            != "mobileworld.runtime.sentinel-r2.5-pilot-model-switch-evidence/v1"
            or value != production_pilot_model_switch_evidence_projection(typed_switch)
            or value.get("transition_sha256") != production_model_handoff_evidence_sha256(handoff)
            or handoff.baseline_shared_gpu_attestation != baseline
        ):
            raise R25PostRunIntegrityError(code, "cleanup switch preimage/order differs")
        transition_stop_roots.append(production_model_stop_evidence_sha256(handoff.source_stop))
    if not all(root in expected_stopped_roots for root in transition_stop_roots):
        raise R25PostRunIntegrityError(code, "pilot switch stops are absent from cleanup")


def _load_completed_sequence(
    output_root: Path,
    *,
    repository_root: Path,
    authority: PostRunIntegrityAuthorityV1,
    stable_directory_fd: int | None = None,
    stable_directory_metadata: os.stat_result | None = None,
) -> _SequenceEvidenceV1:
    if stable_directory_fd is None:
        with _open_stable_owner_directory(
            output_root,
            repository_root=repository_root,
            code="INVALID_SEQUENCE_OUTPUT_ROOT",
        ) as (root, descriptor, metadata):
            return _load_completed_sequence(
                root,
                repository_root=repository_root,
                authority=authority,
                stable_directory_fd=descriptor,
                stable_directory_metadata=metadata,
            )
    if stable_directory_metadata is None:
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_OUTPUT_ROOT", "stable sequence root metadata is absent"
        )
    root = output_root
    root_metadata = stable_directory_metadata
    try:
        entries = frozenset(os.listdir(stable_directory_fd))
    except OSError as exc:
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_OUTPUT_ROOT", "sequence file census failed"
        ) from exc
    if entries != _SEQUENCE_FILES:
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_FILE_CENSUS", "sequence output is not the exact terminal bundle"
        )

    binding_raw, binding_metadata = _read_owner_file_at(
        stable_directory_fd,
        "manifest-binding.json",
        maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
        code="INVALID_SEQUENCE_BINDING",
    )
    binding = _decode_canonical_json(binding_raw, code="INVALID_SEQUENCE_BINDING")
    _require_roots(binding, authority, code="SEQUENCE_BINDING_MISMATCH", name="manifest_binding")
    manifest = authority.run_manifest
    cleanup_bound = _object(
        binding.get("resource_cleanup_upper_bound"),
        code="SEQUENCE_BINDING_MISMATCH",
        name="manifest_binding.resource_cleanup_upper_bound",
    )
    if (
        set(binding) != _EXECUTOR_BINDING_FIELDS
        or binding.get("authorized_stages") != [stage for stage, _ in _STAGE_FILES]
        or binding.get("schema_version") != _LIVE_EXECUTOR_BINDING_SCHEMA_VERSION
        or binding.get("execution_scope") != "R24_R25_FULL"
        or binding.get("production_evidence_required") is not True
        or binding.get("resource_topology") != manifest.resource_topology
        or binding.get("resource_cleanup_upper_bound_seconds")
        != manifest.max_resource_cleanup_wall_time_seconds
        or binding.get("resource_cleanup_upper_bound_sha256")
        != manifest.resource_cleanup_upper_bound_sha256
        or _sha_json(cast(JsonValue, cleanup_bound)) != manifest.resource_cleanup_upper_bound_sha256
        or binding.get("max_model_switches") != manifest.max_model_switches
        or binding.get("max_model_switch_wall_time_seconds")
        != manifest.max_model_switch_wall_time_seconds
        or binding.get("max_total_model_switch_wall_time_seconds")
        != manifest.max_total_model_switch_wall_time_seconds
        or binding.get("pilot_switch_authority_sha256")
        != _expected_pilot_switch_authority_sha256(authority)
    ):
        raise R25PostRunIntegrityError(
            "SEQUENCE_BINDING_MISMATCH", "sequence binding is not the exact full authority"
        )
    source_commit = binding.get("source_commit")
    if source_commit != authority.source_commit:
        raise R25PostRunIntegrityError("SEQUENCE_BINDING_MISMATCH", "source commit is invalid")

    terminal_raw, terminal_metadata = _read_owner_file_at(
        stable_directory_fd,
        "terminal.json",
        maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
        code="INVALID_SEQUENCE_TERMINAL",
    )
    terminal = _decode_canonical_json(terminal_raw, code="INVALID_SEQUENCE_TERMINAL")
    _require_roots(terminal, authority, code="SEQUENCE_TERMINAL_BINDING_MISMATCH", name="terminal")
    terminal_fields = _EXECUTOR_BINDING_FIELDS | {
        "acceptance_status",
        "cleanup_file_sha256",
        "executor_census",
        "executor_census_sha256",
        "handoff_model_switch_count",
        "handoff_model_switch_evidence_sha256",
        "pilot_model_switch_count",
        "result",
        "result_sha256",
        "stage_file_sha256s",
        "status",
        "terminal_output_published",
        "total_model_switch_count",
    }
    if (
        set(terminal) != terminal_fields
        or any(terminal.get(field) != binding[field] for field in _EXECUTOR_BINDING_FIELDS)
        or terminal.get("status") != "COMPLETE"
        or terminal.get("acceptance_status") != "EXECUTION_COMPLETE_INTEGRITY_PENDING"
        or terminal.get("terminal_output_published") is not True
        or terminal.get("source_commit") != source_commit
    ):
        raise R25PostRunIntegrityError(
            "SEQUENCE_NOT_COMPLETE", "successful terminal publication is absent"
        )
    result = _object(
        terminal.get("result"), code="INVALID_SEQUENCE_TERMINAL", name="terminal.result"
    )
    if (
        set(result)
        != {
            "failed_stage",
            "failure_code",
            "manifest_sha256",
            "receipts",
            "run_id",
            "schema_version",
            "status",
        }
        or result.get("schema_version") != _SEQUENCE_RESULT_SCHEMA_VERSION
        or result.get("status") != "COMPLETE"
        or result.get("run_id") != authority.run_id
        or result.get("manifest_sha256") != authority.authority_manifest_sha256
        or result.get("failed_stage") is not None
        or result.get("failure_code") is not None
        or terminal.get("result_sha256") != _sha_json(cast(JsonValue, result))
    ):
        raise R25PostRunIntegrityError("INVALID_SEQUENCE_TERMINAL", "terminal result proof differs")
    terminal_receipts = _list(
        result.get("receipts"),
        code="INVALID_SEQUENCE_TERMINAL",
        name="terminal.result.receipts",
    )
    if len(terminal_receipts) != len(_STAGE_FILES):
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_TERMINAL", "terminal has an incomplete stage receipt census"
        )
    terminal_census = _object(
        terminal.get("executor_census"),
        code="INVALID_SEQUENCE_TERMINAL",
        name="terminal.executor_census",
    )
    terminal_integer_fields = (
        "actor_actions",
        "actor_calls",
        "cleanup_wall_time_ms",
        "cost_usd_micros",
        "openai_calls",
        "secret_leases_acquired",
        "secret_leases_closed",
        "stage_wall_time_ms",
        "wall_time_ms",
    )
    if (
        set(terminal_census) != _TERMINAL_CENSUS_FIELDS
        or any(
            type(terminal_census.get(field)) is not int or cast(int, terminal_census[field]) < 0
            for field in terminal_integer_fields
        )
        or terminal_census.get("cleanup_attempted") is not True
        or terminal_census.get("cleanup_succeeded") is not True
        or terminal_census.get("output_committed") is not True
        or terminal_census.get("state") != "COMPLETE"
        or terminal_census.get("completed_stages") != [stage for stage, _ in _STAGE_FILES]
        or terminal_census.get("secret_leases_acquired") != 3
        or terminal_census.get("secret_leases_closed") != 3
        or cast(int, terminal_census["wall_time_ms"])
        < max(
            cast(int, terminal_census["stage_wall_time_ms"]),
            cast(int, terminal_census["cleanup_wall_time_ms"]),
        )
        or terminal.get("executor_census_sha256") != _sha_json(cast(JsonValue, terminal_census))
    ):
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_TERMINAL", "terminal cleanup/census proof differs"
        )

    claimed_stage_hashes = _object(
        terminal.get("stage_file_sha256s"),
        code="INVALID_SEQUENCE_TERMINAL",
        name="terminal.stage_file_sha256s",
    )
    if set(claimed_stage_hashes) != {stage for stage, _ in _STAGE_FILES}:
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_TERMINAL", "terminal stage hash census differs"
        )
    stages: list[tuple[str, str, dict[str, JsonValue], bytes, os.stat_result]] = []
    pilot_evidence: dict[str, JsonValue] | None = None
    smoke_evidence: list[dict[str, JsonValue]] = []
    resource_evidence: ProductionResourceStageEvidenceV1 | None = None
    initial_handoff: ProductionModelHandoffEvidenceV1 | None = None
    for index, (stage, filename) in enumerate(_STAGE_FILES):
        stage_raw, stage_metadata = _read_owner_file_at(
            stable_directory_fd,
            filename,
            maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
            code="INVALID_STAGE_ARTIFACT",
        )
        if claimed_stage_hashes.get(stage) != _sha_bytes(stage_raw):
            raise R25PostRunIntegrityError(
                "STAGE_HASH_MISMATCH", f"{stage} stage file differs from terminal"
            )
        document = _decode_canonical_json(stage_raw, code="INVALID_STAGE_ARTIFACT")
        _require_roots(document, authority, code="STAGE_BINDING_MISMATCH", name=f"stage[{stage}]")
        if set(document) != _EXECUTOR_BINDING_FIELDS | {"evidence", "receipt"} or any(
            document.get(field) != binding[field] for field in _EXECUTOR_BINDING_FIELDS
        ):
            raise R25PostRunIntegrityError(
                "STAGE_BINDING_MISMATCH", f"{stage} transaction binding differs"
            )
        evidence = _object(
            document.get("evidence"), code="INVALID_STAGE_ARTIFACT", name="stage.evidence"
        )
        receipt = _object(
            document.get("receipt"), code="INVALID_STAGE_ARTIFACT", name="stage.receipt"
        )
        _require_stage_receipt(receipt, stage=stage, authority=authority)
        if (
            receipt.get("stage") != stage
            or receipt.get("passed") is not True
            or receipt.get("manifest_sha256") != authority.authority_manifest_sha256
            or receipt.get("evidence_sha256") != _sha_json(cast(JsonValue, evidence))
            or terminal_receipts[index] != receipt
        ):
            raise R25PostRunIntegrityError(
                "STAGE_BINDING_MISMATCH", f"{stage} receipt/evidence differs"
            )
        if stage == "RESOURCE_PREFLIGHT":
            resource_evidence = validate_resource_preflight_stage_evidence_projection_v1(
                evidence, authority=authority
            )
        elif stage in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}:
            smoke_projection = evidence
            if stage == "MAI_LIVE_SMOKE" and authority.run_manifest.resource_topology == (
                "SINGLE_GPU_SEQUENTIAL_SHARED"
            ):
                wrapper = evidence
                if (
                    set(wrapper)
                    != {
                        "domain",
                        "handoff_evidence",
                        "handoff_evidence_sha256",
                        "manifest_sha256",
                        "smoke_evidence",
                        "smoke_evidence_sha256",
                    }
                    or wrapper.get("domain") != "r24-r25-full-mai-stage-evidence"
                    or wrapper.get("manifest_sha256") != authority.authority_manifest_sha256
                ):
                    raise R25PostRunIntegrityError(
                        "INVALID_SMOKE_STAGE", "MAI handoff wrapper differs"
                    )
                smoke_projection = _object(
                    wrapper.get("smoke_evidence"),
                    code="INVALID_SMOKE_STAGE",
                    name="mai_stage.smoke_evidence",
                )
                handoff_envelope = _object(
                    wrapper.get("handoff_evidence"),
                    code="INVALID_MODEL_HANDOFF_EVIDENCE",
                    name="mai_stage.handoff_evidence",
                )
                if (
                    wrapper.get("smoke_evidence_sha256")
                    != _sha_json(cast(JsonValue, smoke_projection))
                    or wrapper.get("handoff_evidence_sha256")
                    != _sha_json(cast(JsonValue, handoff_envelope))
                    or set(handoff_envelope) != {"domain", "schema_version", "value"}
                    or handoff_envelope.get("domain") != "production-model-handoff-evidence"
                    or handoff_envelope.get("schema_version")
                    != "mobileworld.runtime.sentinel-r2.4-model-handoff-evidence/v1"
                    or resource_evidence is None
                    or len(resource_evidence.gpu_lease_sha256s) != 1
                ):
                    raise R25PostRunIntegrityError(
                        "INVALID_MODEL_HANDOFF_EVIDENCE", "initial handoff envelope differs"
                    )
                initial_handoff = validate_production_model_handoff_evidence_projection_v1(
                    handoff_envelope.get("value"),
                    expected_manifest_sha256=authority.authority_manifest_sha256,
                    expected_runtime_config_sha256=authority.runtime_config_sha256,
                    expected_source_host=PilotHostV1.QWEN3_VL,
                    expected_target_host=PilotHostV1.MAI_UI,
                    expected_gpu_lease_sha256=resource_evidence.gpu_lease_sha256s[0],
                )
            elif stage == "MAI_LIVE_SMOKE" and set(evidence) != {
                "actor_resource_sha256",
                "cases",
                "census",
                "history_policy_stage_sha256",
                "host",
                "manifest_sha256",
                "run_id",
                "schema_version",
                "stage",
            }:
                raise R25PostRunIntegrityError(
                    "INVALID_SMOKE_STAGE", "concurrent MAI smoke evidence differs"
                )
            validated_smoke = validate_smoke_stage_durable_evidence_projection_v2(
                smoke_projection, authority=authority, expected_stage=stage
            )
            smoke_evidence.append(validated_smoke)
            _require_receipt_evidence_census(
                receipt,
                _object(
                    validated_smoke.get("census"),
                    code="INVALID_SMOKE_STAGE",
                    name="smoke.census",
                ),
                stage=stage,
            )
        elif stage == "R25_PILOT":
            pilot_evidence = evidence
        stages.append((stage, filename, document, stage_raw, stage_metadata))
    if resource_evidence is None or pilot_evidence is None or len(smoke_evidence) != 2:
        raise R25PostRunIntegrityError("INVALID_PILOT_STAGE", "pilot evidence is absent")
    pilot_evidence = validate_pilot_stage_durable_evidence_projection_v2(
        pilot_evidence,
        expected_run_manifest=authority.run_manifest,
        expected_manifest_sha256=authority.authority_manifest_sha256,
        expected_resolved_pilot_inputs_sha256=(authority.resolved_pilot_inputs_sha256),
        expected_backend_endpoint=authority.backend_endpoint,
        expected_preflight_report_sha256=authority.preflight_report_sha256,
        expected_factory_binding_sha256=authority.factory_binding_sha256,
    )
    pilot_receipt = _object(
        stages[-1][2].get("receipt"),
        code="INVALID_STAGE_ARTIFACT",
        name="pilot.receipt",
    )
    _require_receipt_evidence_census(
        pilot_receipt,
        _object(
            pilot_evidence.get("census"),
            code="INVALID_PILOT_STAGE",
            name="pilot.census",
        ),
        stage="R25_PILOT",
    )
    stage_receipts = [
        _object(document.get("receipt"), code="INVALID_STAGE_ARTIFACT", name="stage.receipt")
        for _, _, document, _, _ in stages
    ]
    if (
        any(
            terminal_census[field] != sum(cast(int, receipt[field]) for receipt in stage_receipts)
            for field in ("actor_actions", "actor_calls", "cost_usd_micros", "openai_calls")
        )
        or terminal_census["stage_wall_time_ms"]
        != sum(cast(int, receipt["wall_time_ms"]) for receipt in stage_receipts)
        or cast(int, terminal_census["wall_time_ms"])
        > (authority.max_sequence_wall_time_seconds - authority.max_wall_time_seconds) * 1000
    ):
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_TERMINAL", "terminal receipt/census aggregate differs"
        )
    cells = _list(pilot_evidence.get("cells"), code="INVALID_PILOT_STAGE", name="pilot.cells")
    if len(cells) != authority.expected_cell_count:
        raise R25PostRunIntegrityError(
            "INVALID_PILOT_CELL_CENSUS", "pilot cell count differs from authority"
        )
    for index, cell_value in enumerate(cells):
        cell = _object(cell_value, code="INVALID_PILOT_STAGE", name="pilot.cell")
        if (
            cell.get("sequence_index") != index
            or cell.get("manifest_sha256") != authority.authority_manifest_sha256
            or cell.get("run_id") != authority.run_id
        ):
            raise R25PostRunIntegrityError(
                "INVALID_PILOT_CELL_CENSUS", "pilot cell identity/order differs"
            )

    cleanup_raw, cleanup_metadata = _read_owner_file_at(
        stable_directory_fd,
        "04-resource-cleanup.json",
        maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
        code="INVALID_RESOURCE_CLEANUP",
    )
    if terminal.get("cleanup_file_sha256") != _sha_bytes(cleanup_raw):
        raise R25PostRunIntegrityError(
            "RESOURCE_CLEANUP_HASH_MISMATCH", "cleanup file differs from terminal"
        )
    cleanup = _decode_canonical_json(cleanup_raw, code="INVALID_RESOURCE_CLEANUP")
    _require_roots(cleanup, authority, code="RESOURCE_CLEANUP_BINDING_MISMATCH", name="cleanup")
    if set(cleanup) != _EXECUTOR_BINDING_FIELDS | {
        "resource_cleanup_evidence",
        "resource_cleanup_evidence_sha256",
        "resource_cleanup_status",
    } or any(cleanup.get(field) != binding[field] for field in _EXECUTOR_BINDING_FIELDS):
        raise R25PostRunIntegrityError(
            "RESOURCE_CLEANUP_BINDING_MISMATCH", "cleanup transaction binding differs"
        )
    cleanup_evidence = _object(
        cleanup.get("resource_cleanup_evidence"),
        code="INVALID_RESOURCE_CLEANUP",
        name="cleanup.resource_cleanup_evidence",
    )
    cleanup_evidence_sha256 = _sha_json(cast(JsonValue, cleanup_evidence))
    if (
        cleanup.get("resource_cleanup_status") != "SUCCEEDED"
        or cleanup.get("resource_cleanup_evidence_sha256") != cleanup_evidence_sha256
        or terminal_census.get("cleanup_evidence_sha256") != cleanup_evidence_sha256
    ):
        raise R25PostRunIntegrityError(
            "RESOURCE_CLEANUP_NOT_PROVEN", "successful cleanup proof differs"
        )
    _require_pilot_switch_cleanup_bridge(
        pilot_evidence=pilot_evidence,
        cleanup_evidence=cleanup_evidence,
        authority=authority,
        resource_evidence=resource_evidence,
        initial_handoff=initial_handoff,
    )
    expected_handoff_count = (
        1 if authority.run_manifest.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED" else 0
    )
    expected_pilot_switch_count = cast(int, authority.run_manifest.max_model_switches) - (
        expected_handoff_count
    )
    expected_handoff_sha256 = (
        None
        if initial_handoff is None
        else production_model_handoff_evidence_sha256(initial_handoff)
    )
    if (
        terminal.get("handoff_model_switch_count") != expected_handoff_count
        or terminal.get("pilot_model_switch_count") != expected_pilot_switch_count
        or terminal.get("total_model_switch_count") != authority.run_manifest.max_model_switches
        or terminal.get("handoff_model_switch_evidence_sha256") != expected_handoff_sha256
    ):
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_TERMINAL", "terminal model-switch census differs"
        )

    return _SequenceEvidenceV1(
        output_root=root,
        output_root_identity=_path_identity(root, root_metadata, kind="DIRECTORY"),
        source_commit=cast(str, source_commit),
        binding_raw=binding_raw,
        binding_metadata=binding_metadata,
        terminal=terminal,
        terminal_raw=terminal_raw,
        terminal_metadata=terminal_metadata,
        cleanup=cleanup,
        cleanup_raw=cleanup_raw,
        cleanup_metadata=cleanup_metadata,
        stages=tuple(stages),
        smoke_evidence=cast(
            tuple[dict[str, JsonValue], dict[str, JsonValue]], tuple(smoke_evidence)
        ),
        pilot_evidence=pilot_evidence,
    )


def _production_hash(domain: str, value: JsonValue) -> str:
    return _sha_json(cast(JsonValue, _production_preimage(domain, value)))


def _pilot_effective_reset_match_projection(
    value: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "reset_seed": value["reset_seed"],
        "task_goal_sha256": value["task_goal_sha256"],
        "task_id": value["task_id"],
        "task_name": value["task_name"],
        "task_parameters_sha256": value["task_parameters_sha256"],
        "trial": value["trial"],
    }


def _read_json_lines(path: Path, *, maximum_bytes: int, code: str) -> list[dict[str, JsonValue]]:
    raw, _ = _read_owner_file(
        path,
        maximum_bytes=maximum_bytes,
        code=code,
        require_canonical=False,
    )
    if not raw.endswith(b"\n"):
        raise R25PostRunIntegrityError(code, "JSONL stream has no terminal newline")
    documents: list[dict[str, JsonValue]] = []
    for line in raw.splitlines():
        if not line:
            raise R25PostRunIntegrityError(code, "JSONL stream has an empty line")
        documents.append(_decode_canonical_json(line, code=code))
    if not documents:
        raise R25PostRunIntegrityError(code, "JSONL stream is empty")
    return documents


def _read_json_lines_at(
    directory_fd: int,
    relative_path: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> list[dict[str, JsonValue]]:
    raw, _ = _read_owner_relative_file_at(
        directory_fd,
        relative_path,
        maximum_bytes=maximum_bytes,
        code=code,
        require_canonical=False,
    )
    if not raw.endswith(b"\n"):
        raise R25PostRunIntegrityError(code, "JSONL stream has no terminal newline")
    documents: list[dict[str, JsonValue]] = []
    for line in raw.splitlines():
        if not line:
            raise R25PostRunIntegrityError(code, "JSONL stream has an empty line")
        documents.append(_decode_canonical_json(line, code=code))
    if not documents:
        raise R25PostRunIntegrityError(code, "JSONL stream is empty")
    return documents


def _single_event(events: list[dict[str, JsonValue]], event_type: str) -> dict[str, JsonValue]:
    matches = [event for event in events if event.get("event_type") == event_type]
    if len(matches) != 1:
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH",
            f"Collector needs exactly one {event_type} event",
        )
    return matches[0]


def _blob_digest_from_initial_step(run_root_fd: int, step_started: dict[str, JsonValue]) -> str:
    payload = _object(
        step_started.get("payload"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="step_started.payload",
    )
    observation = _object(
        payload.get("observation"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="step_started.observation",
    )
    screenshot = _object(
        observation.get("screenshot"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="step_started.screenshot",
    )
    # The production driver provides the exact PNG bytes used to form the
    # reset commitment, so source_blob is the authoritative bridge.  The
    # canonical pixel blob remains independently checked by Collector v1.
    reference = _object(
        screenshot.get("source_blob"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="step_started.screenshot.source_blob",
    )
    digest = reference.get("digest")
    byte_length = reference.get("byte_length")
    relative_path = reference.get("relative_path")
    if (
        reference.get("algorithm") != "sha256"
        or type(digest) is not str
        or _SHA256.fullmatch(digest) is None
        or type(byte_length) is not int
        or not 1 <= byte_length <= 64 * 1024 * 1024
        or relative_path != f"blobs/sha256/{digest[:2]}/{digest}"
    ):
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH", "initial screenshot blob differs"
        )
    blob_raw, _ = _read_owner_relative_file_at(
        run_root_fd,
        Path(cast(str, relative_path)),
        maximum_bytes=64 * 1024 * 1024,
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        require_canonical=False,
    )
    if len(blob_raw) != byte_length or _sha_bytes(blob_raw) != digest:
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH", "initial screenshot bytes differ"
        )
    return digest


def _require_reset_semantics(
    locator: CollectorRunLocatorV1,
    *,
    task_goal: str,
    screenshot_sha256: str,
) -> None:
    reset = locator.reset_evidence
    required = {
        "backend_endpoint",
        "case_id",
        "effective_reset_state",
        "effective_reset_state_sha256",
        "manifest_sha256",
        "observation_screenshot_sha256",
        "resolved_inputs_sha256",
        "reset_seed",
        "resource_switch_evidence_sha256",
        "task_id",
        "task_name",
        "task_parameters_sha256",
        "trial",
    }
    if set(reset) != required:
        raise R25PostRunIntegrityError(
            "RESET_COLLECTOR_BINDING_MISMATCH", "reset evidence fields differ"
        )
    effective = _object(
        reset.get("effective_reset_state"),
        code="RESET_COLLECTOR_BINDING_MISMATCH",
        name="reset.effective_reset_state",
    )
    effective_fields = {
        "observation_screenshot_sha256",
        "reset_seed",
        "task_goal_sha256",
        "task_id",
        "task_name",
        "task_parameters_sha256",
        "trial",
    }
    if (
        set(effective) != effective_fields
        or type(reset["reset_seed"]) is not int
        or not 0 <= reset["reset_seed"] <= 2_147_483_647
        or type(reset["trial"]) is not int
        or reset["trial"] != 1
        or type(reset["task_id"]) is not str
        or type(reset["task_name"]) is not str
        or type(reset["task_parameters_sha256"]) is not str
        or _SHA256.fullmatch(reset["task_parameters_sha256"]) is None
        or type(reset["effective_reset_state_sha256"]) is not str
        or _SHA256.fullmatch(reset["effective_reset_state_sha256"]) is None
        or type(reset["resolved_inputs_sha256"]) is not str
        or _SHA256.fullmatch(reset["resolved_inputs_sha256"]) is None
        or (
            locator.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            and locator.sequence_index % 2 == 0
            and (
                type(reset["resource_switch_evidence_sha256"]) is not str
                or _SHA256.fullmatch(reset["resource_switch_evidence_sha256"]) is None
            )
        )
        or (
            (
                locator.resource_topology == "INDEPENDENT_GPU_CONCURRENT"
                or locator.sequence_index % 2 == 1
            )
            and reset["resource_switch_evidence_sha256"] is not None
        )
        or reset["backend_endpoint"] != locator.backend_endpoint
    ):
        raise R25PostRunIntegrityError(
            "RESET_COLLECTOR_BINDING_MISMATCH", "reset evidence types differ"
        )
    expected_effective: dict[str, JsonValue] = {
        "observation_screenshot_sha256": screenshot_sha256,
        "reset_seed": reset["reset_seed"],
        "task_goal_sha256": hashlib.sha256(task_goal.encode("utf-8")).hexdigest(),
        "task_id": reset["task_id"],
        "task_name": reset["task_name"],
        "task_parameters_sha256": reset["task_parameters_sha256"],
        "trial": reset["trial"],
    }
    if (
        effective != expected_effective
        or reset.get("manifest_sha256") != locator.manifest_sha256
        or reset.get("case_id") != f"pilot-cell-{locator.sequence_index:03d}"
        or reset.get("task_id") != locator.task_id
        or reset.get("task_name") != locator.task_id
        or reset.get("observation_screenshot_sha256") != screenshot_sha256
        or reset.get("effective_reset_state_sha256")
        != _production_hash(
            "production-pilot-effective-reset-state",
            cast(
                JsonValue,
                _pilot_effective_reset_match_projection(expected_effective),
            ),
        )
    ):
        raise R25PostRunIntegrityError(
            "RESET_COLLECTOR_BINDING_MISMATCH", "reset commitment differs from Collector"
        )


def _official_score_from_hex(value: object) -> float | None:
    if type(value) is not str:
        return None
    try:
        score = float.fromhex(value)
    except ValueError:
        return None
    if not math.isfinite(score) or not 0.0 <= score <= 1.0 or score.hex() != value:
        return None
    return score


def _require_official_result_semantics(
    locator: CollectorRunLocatorV1,
    task_ended: dict[str, JsonValue],
) -> None:
    payload = _object(
        task_ended.get("payload"),
        code="OFFICIAL_RESULT_COLLECTOR_BINDING_MISMATCH",
        name="task_ended.payload",
    )
    evaluation = _object(
        payload.get("environment_evaluation"),
        code="OFFICIAL_RESULT_COLLECTOR_BINDING_MISMATCH",
        name="task_ended.environment_evaluation",
    )
    teardown = _object(
        payload.get("teardown"),
        code="OFFICIAL_RESULT_COLLECTOR_BINDING_MISMATCH",
        name="task_ended.teardown",
    )
    score = evaluation.get("score")
    reason = evaluation.get("reason")
    if (
        payload.get("runtime_status") != "completed"
        or payload.get("capture_complete") is not True
        or type(score) is not float
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
        or type(reason) is not str
        or evaluation.get("exception") is not None
        or teardown.get("returned") is not True
        or teardown.get("exception") is not None
    ):
        raise R25PostRunIntegrityError(
            "OFFICIAL_RESULT_COLLECTOR_BINDING_MISMATCH",
            "Collector task terminal is not a completed scored cleanup",
        )
    score_float_hex = score.hex()
    score_ppm = round(score * 1_000_000)
    reason_sha256 = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    official = locator.official_result
    evidence = locator.official_result_evidence
    evidence_value: dict[str, JsonValue] = {
        "evaluator_id": _OFFICIAL_RESULT_EVALUATOR_ID,
        "official_success_metric_id": OFFICIAL_SUCCESS_METRIC_ID_V1,
        "official_success_operator": OFFICIAL_SUCCESS_OPERATOR_V1,
        "official_success_threshold_float_hex": OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1,
        "reason": reason,
        "reason_sha256": reason_sha256,
        "score_float_hex": score_float_hex,
        "score_ppm": score_ppm,
        "task_id": locator.task_id,
    }
    if (
        set(evidence) != set(evidence_value)
        or evidence != evidence_value
        or set(official)
        != {
            "evaluator_id",
            "official_success_metric_id",
            "official_success_operator",
            "official_success_threshold_float_hex",
            "reason_sha256",
            "result_payload_sha256",
            "score_float_hex",
            "score_ppm",
            "successful",
            "task_id",
        }
        or official.get("task_id") != locator.task_id
        or official.get("evaluator_id") != _OFFICIAL_RESULT_EVALUATOR_ID
        or official.get("official_success_metric_id") != OFFICIAL_SUCCESS_METRIC_ID_V1
        or official.get("official_success_operator") != OFFICIAL_SUCCESS_OPERATOR_V1
        or official.get("official_success_threshold_float_hex")
        != OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1
        or official.get("score_float_hex") != score_float_hex
        or official.get("score_ppm") != score_ppm
        or official.get("successful")
        is not (score > float.fromhex(OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1))
        or official.get("reason_sha256") != reason_sha256
        or official.get("result_payload_sha256") != locator.official_result_evidence_sha256
        or locator.official_result_evidence_sha256
        != _production_hash("production-official-result", evidence_value)
    ):
        raise R25PostRunIntegrityError(
            "OFFICIAL_RESULT_COLLECTOR_BINDING_MISMATCH",
            "derived official result differs from Collector terminal",
        )


def _validate_collector_run_semantics(
    locator: CollectorRunLocatorV1,
    *,
    repository_root: Path,
    stable_directory_fd: int | None = None,
    stable_directory_metadata: os.stat_result | None = None,
) -> tuple[Path, dict[str, JsonValue], dict[str, JsonValue], os.stat_result]:
    if stable_directory_fd is None:
        with _open_stable_owner_directory(
            Path(locator.run_root),
            repository_root=repository_root,
            code="INVALID_COLLECTOR_RUN_ROOT",
        ) as (run_root, descriptor, metadata):
            return _validate_collector_run_semantics(
                locator,
                repository_root=repository_root,
                stable_directory_fd=descriptor,
                stable_directory_metadata=metadata,
            )
    if stable_directory_metadata is None:
        raise R25PostRunIntegrityError(
            "INVALID_COLLECTOR_RUN_ROOT", "stable Collector root metadata is absent"
        )
    run_root = Path(locator.run_root)
    run_metadata = stable_directory_metadata
    manifest_raw, manifest_metadata = _read_owner_file_at(
        stable_directory_fd,
        "manifest.final.json",
        maximum_bytes=_MAX_COLLECTOR_MANIFEST_BYTES,
        code="INVALID_COLLECTOR_MANIFEST",
        require_canonical=False,
    )
    manifest = _decode_canonical_json_line(manifest_raw, code="INVALID_COLLECTOR_MANIFEST")
    if (
        len(manifest_raw) != locator.manifest_final_byte_count
        or _sha_bytes(manifest_raw) != locator.manifest_final_sha256
        or manifest.get("run_id") != locator.collector_run_id
        or manifest.get("runtime_status") != "completed"
        or manifest.get("capture_complete") is not True
        or manifest.get("missing_artifacts") != []
        or manifest.get("collector_error_event_ids") != []
    ):
        raise R25PostRunIntegrityError(
            "COLLECTOR_MANIFEST_BINDING_MISMATCH", "Collector final manifest differs"
        )
    streams = _list(
        manifest.get("task_streams"),
        code="COLLECTOR_MANIFEST_BINDING_MISMATCH",
        name="manifest.task_streams",
    )
    if len(streams) != 1:
        raise R25PostRunIntegrityError(
            "COLLECTOR_MANIFEST_BINDING_MISMATCH", "Collector run is not one exact cell"
        )
    stream = _object(streams[0], code="COLLECTOR_MANIFEST_BINDING_MISMATCH", name="task_stream")
    relative_path = f"tasks/{locator.task_run_id}/events.jsonl"
    if (
        stream.get("task_run_id") != locator.task_run_id
        or stream.get("relative_path") != relative_path
        or stream.get("runtime_status") != "completed"
        or stream.get("capture_complete") is not True
        or stream.get("missing_artifacts") != []
        or stream.get("collector_error_event_ids") != []
    ):
        raise R25PostRunIntegrityError(
            "COLLECTOR_MANIFEST_BINDING_MISMATCH", "Collector task stream differs"
        )
    events = _read_json_lines_at(
        stable_directory_fd,
        Path(relative_path),
        maximum_bytes=512 * 1024 * 1024,
        code="INVALID_COLLECTOR_TASK_STREAM",
    )
    task_started = _single_event(events, "task_started")
    task_ended = _single_event(events, "task_ended")
    started_payload = _object(
        task_started.get("payload"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="task_started.payload",
    )
    task_goal = started_payload.get("task_goal")
    if (
        task_started.get("task_run_id") != locator.task_run_id
        or task_ended.get("task_run_id") != locator.task_run_id
        or started_payload.get("task_name") != locator.task_id
        or type(task_goal) is not str
        or not task_goal
    ):
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH", "Collector task identity/goal differs"
        )
    steps = [event for event in events if event.get("event_type") == "step_started"]
    if not steps:
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH", "Collector has no initial step"
        )
    initial_payload = _object(
        steps[0].get("payload"),
        code="COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="initial_step.payload",
    )
    if initial_payload.get("step_index") != 1:
        raise R25PostRunIntegrityError(
            "COLLECTOR_SEMANTIC_BINDING_MISMATCH", "Collector initial step index differs"
        )
    screenshot_sha256 = _blob_digest_from_initial_step(stable_directory_fd, steps[0])
    _require_reset_semantics(locator, task_goal=task_goal, screenshot_sha256=screenshot_sha256)
    _require_official_result_semantics(locator, task_ended)
    cleanup = locator.cleanup_evidence
    teardown_result = _object(
        cleanup.get("teardown_result"),
        code="CLEANUP_COLLECTOR_BINDING_MISMATCH",
        name="cleanup.teardown_result",
    )
    deadline_binding = _object(
        cleanup.get("deadline_binding"),
        code="CLEANUP_COLLECTOR_BINDING_MISMATCH",
        name="cleanup.deadline_binding",
    )
    if (
        cleanup.get("cleanup_dispatch_authorized") is not True
        or cleanup.get("teardown_attempted") is not True
        or cleanup.get("unit_id") != locator.unit_id
        or cleanup.get("task_run_id") != locator.task_run_id
        or cleanup.get("collector_manifest_sha256") != locator.manifest_final_sha256
        or cleanup.get("unit_journal_sha256") != locator.unit_journal_sha256
        or set(teardown_result)
        != {"message", "message_sha256", "request_dispatched", "status", "task_name"}
        or type(teardown_result.get("message")) is not str
        or teardown_result.get("message_sha256")
        != hashlib.sha256(cast(str, teardown_result["message"]).encode("utf-8")).hexdigest()
        or teardown_result.get("request_dispatched") is not True
        or teardown_result.get("status") != "SUCCEEDED"
        or teardown_result.get("task_name") != locator.task_id
        or cleanup.get("teardown_result_sha256")
        != _production_hash("production-task-teardown-result", cast(JsonValue, teardown_result))
        or not deadline_binding
    ):
        raise R25PostRunIntegrityError(
            "CLEANUP_COLLECTOR_BINDING_MISMATCH", "cleanup locator/preimage differs"
        )
    return (
        run_root,
        manifest,
        _path_identity(Path(locator.manifest_final_path), manifest_metadata, kind="REGULAR_FILE"),
        run_metadata,
    )


def _validate_smoke_collector_run_semantics(
    locator: SmokeCollectorRunLocatorV1,
    *,
    repository_root: Path,
    stable_directory_fd: int | None = None,
    stable_directory_metadata: os.stat_result | None = None,
) -> tuple[Path, dict[str, JsonValue], dict[str, JsonValue], os.stat_result]:
    """Bind a scoreless parser smoke to an aborted task in a complete raw run."""

    if stable_directory_fd is None:
        with _open_stable_owner_directory(
            Path(locator.run_root),
            repository_root=repository_root,
            code="INVALID_SMOKE_COLLECTOR_RUN_ROOT",
        ) as (run_root, descriptor, metadata):
            return _validate_smoke_collector_run_semantics(
                locator,
                repository_root=repository_root,
                stable_directory_fd=descriptor,
                stable_directory_metadata=metadata,
            )
    if stable_directory_metadata is None:
        raise R25PostRunIntegrityError(
            "INVALID_SMOKE_COLLECTOR_RUN_ROOT", "stable smoke Collector root is absent"
        )
    run_root = Path(locator.run_root)
    manifest_raw, manifest_metadata = _read_owner_file_at(
        stable_directory_fd,
        "manifest.final.json",
        maximum_bytes=_MAX_COLLECTOR_MANIFEST_BYTES,
        code="INVALID_SMOKE_COLLECTOR_MANIFEST",
        require_canonical=False,
    )
    manifest = _decode_canonical_json_line(manifest_raw, code="INVALID_SMOKE_COLLECTOR_MANIFEST")
    streams = _list(
        manifest.get("task_streams"),
        code="SMOKE_COLLECTOR_MANIFEST_BINDING_MISMATCH",
        name="manifest.task_streams",
    )
    if (
        len(manifest_raw) != locator.manifest_final_byte_count
        or _sha_bytes(manifest_raw) != locator.manifest_final_sha256
        or manifest.get("run_id") != locator.collector_run_id
        or manifest.get("runtime_status") != "completed"
        or manifest.get("capture_complete") is not True
        or manifest.get("missing_artifacts") != []
        or manifest.get("collector_error_event_ids") != []
        or len(streams) != 1
    ):
        raise R25PostRunIntegrityError(
            "SMOKE_COLLECTOR_MANIFEST_BINDING_MISMATCH", "smoke final manifest differs"
        )
    stream = _object(
        streams[0], code="SMOKE_COLLECTOR_MANIFEST_BINDING_MISMATCH", name="task_stream"
    )
    relative_path = f"tasks/{locator.task_run_id}/events.jsonl"
    if (
        stream.get("task_run_id") != locator.task_run_id
        or stream.get("relative_path") != relative_path
        or stream.get("runtime_status") != "aborted"
        or stream.get("capture_complete") is not True
        or stream.get("missing_artifacts") != []
        or stream.get("collector_error_event_ids") != []
    ):
        raise R25PostRunIntegrityError(
            "SMOKE_COLLECTOR_MANIFEST_BINDING_MISMATCH", "smoke task stream differs"
        )
    events = _read_json_lines_at(
        stable_directory_fd,
        Path(relative_path),
        maximum_bytes=512 * 1024 * 1024,
        code="INVALID_SMOKE_COLLECTOR_TASK_STREAM",
    )
    task_started = _single_event(events, "task_started")
    task_ended = _single_event(events, "task_ended")
    started = _object(
        task_started.get("payload"),
        code="SMOKE_COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="task_started.payload",
    )
    ended = _object(
        task_ended.get("payload"),
        code="SMOKE_COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="task_ended.payload",
    )
    evaluation = _object(
        ended.get("environment_evaluation"),
        code="SMOKE_COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="task_ended.environment_evaluation",
    )
    termination = _object(
        ended.get("termination"),
        code="SMOKE_COLLECTOR_SEMANTIC_BINDING_MISMATCH",
        name="task_ended.termination",
    )
    event_types = [event.get("event_type") for event in events]
    if (
        task_started.get("task_run_id") != locator.task_run_id
        or task_ended.get("task_run_id") != locator.task_run_id
        or started.get("task_name") != locator.task_id
        or type(started.get("task_goal")) is not str
        or started.get("task_goal") == ""
        or ended.get("runtime_status") != "aborted"
        or ended.get("capture_complete") is not True
        or evaluation != {"exception": None, "reason": None, "score": None}
        or termination.get("source") != "r2_4_parser_smoke_no_action"
        or termination.get("step_index") != 1
        or termination.get("exception") is not None
        or event_types.count("agent_decision") != 1
        or event_types.count("transition_not_executed") != 1
        or "action_execution_started" in event_types
        or "transition_completed" in event_types
        or "transition_failed" in event_types
        or locator.census.get("actor_actions") != 0
        or locator.decision.get("executed_action_sha256") is not None
    ):
        raise R25PostRunIntegrityError(
            "SMOKE_COLLECTOR_SEMANTIC_BINDING_MISMATCH",
            "scoreless action-free parser smoke semantics differ",
        )
    return (
        run_root,
        manifest,
        _path_identity(Path(locator.manifest_final_path), manifest_metadata, kind="REGULAR_FILE"),
        stable_directory_metadata,
    )


def _file_record(path: Path, raw: bytes, metadata: os.stat_result) -> dict[str, JsonValue]:
    return {
        "byte_count": len(raw),
        "path_identity": cast(JsonValue, _path_identity(path, metadata, kind="REGULAR_FILE")),
        "sha256": _sha_bytes(raw),
    }


def derived_post_run_integrity_root_v1(sequence_output_root: Path) -> Path:
    if not sequence_output_root.is_absolute() or sequence_output_root.name in {"", ".", ".."}:
        raise R25PostRunIntegrityError(
            "INVALID_SEQUENCE_OUTPUT_ROOT", "sequence output root is not absolute"
        )
    return sequence_output_root.with_name(sequence_output_root.name + ".post-run-integrity")


@contextmanager
def _create_fresh_report_root(
    sequence_output_root: Path,
    *,
    repository_root: Path,
) -> Iterator[tuple[Path, int, os.stat_result]]:
    target = derived_post_run_integrity_root_v1(sequence_output_root)
    descriptor = -1
    try:
        with _open_stable_owner_directory(
            target.parent,
            repository_root=repository_root,
            code="INVALID_INTEGRITY_REPORT_PARENT",
        ) as (parent, parent_fd, _):
            try:
                os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise R25PostRunIntegrityError(
                    "INVALID_INTEGRITY_REPORT_ROOT",
                    "derived report root already exists",
                )
            os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
            descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
            ):
                raise OSError("created report root metadata differs")
            os.fsync(parent_fd)
        yield parent / target.name, descriptor, metadata
        current = os.fstat(descriptor)
        path_metadata = target.lstat()
        if (
            (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            or stat.S_ISLNK(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise R25PostRunIntegrityError(
                "INVALID_INTEGRITY_REPORT_ROOT", "derived report root identity changed"
            )
    except R25PostRunIntegrityError:
        raise
    except OSError as exc:
        raise R25PostRunIntegrityError(
            "INTEGRITY_REPORT_PUBLICATION_FAILED", "fresh report root creation failed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _report_projection(report: object) -> dict[str, JsonValue]:
    value = _object(report, code="INVALID_COLLECTOR_INTEGRITY_REPORT", name="integrity_report")
    if set(value) != {
        "checked_at",
        "checker_version",
        "counts",
        "errors",
        "valid",
        "warnings",
    }:
        raise R25PostRunIntegrityError(
            "INVALID_COLLECTOR_INTEGRITY_REPORT", "official report fields differ"
        )
    if (
        value.get("checker_version") != CHECKER_VERSION
        or value.get("valid") is not True
        or value.get("errors") != []
        or value.get("warnings") != []
        or type(value.get("counts")) is not dict
        or type(value.get("checked_at")) is not str
    ):
        raise R25PostRunIntegrityError(
            "COLLECTOR_INTEGRITY_REJECTED",
            "Collector checker requires valid=true with no errors or warnings",
        )
    return value


def _descriptor_bound_official_integrity_check(
    directory_fd: int, *, collector_run_id: str
) -> dict[str, JsonValue]:
    """Run the official checker implementation against the already-open root."""

    bound_root = _BoundCollectorPath.build(directory_fd, collector_run_id)
    checker = IntegrityChecker(bound_root)
    # IntegrityChecker normalizes its constructor argument through Path(), so
    # restore the descriptor-bound subclass before any evidence is opened.
    checker.run_root = bound_root
    checker.blob_store = BlobStore(bound_root)
    checker.artifact_serializer = ArtifactSerializer(checker.blob_store)
    return _object(
        checker.check(),
        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
        name="descriptor_bound_integrity_report",
    )


def _official_checker_worker(
    connection: Connection,
    *,
    directory_fd: int,
    collector_run_id: str,
    public_path: str | None,
) -> None:
    try:
        report = (
            check_run_integrity(Path(public_path))
            if public_path is not None
            else _descriptor_bound_official_integrity_check(
                directory_fd, collector_run_id=collector_run_id
            )
        )
        connection.send(("OK", report))
    except BaseException as exc:
        connection.send(("ERROR", type(exc).__name__))
    finally:
        connection.close()


def _bounded_official_integrity_check(
    directory_fd: int,
    *,
    collector_run_id: str,
    deadline_ns: int,
    public_path: Path | None,
) -> dict[str, JsonValue]:
    """Run one official checker in a killable child under an absolute deadline."""

    checker_deadline_ns = deadline_ns - _CHECKER_REAP_RESERVE_NS
    _require_time(checker_deadline_ns)
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_official_checker_worker,
        kwargs={
            "connection": send,
            "directory_fd": directory_fd,
            "collector_run_id": collector_run_id,
            "public_path": None if public_path is None else str(public_path),
        },
        daemon=False,
    )
    process.start()
    send.close()

    def stop_without_extending_deadline() -> None:
        if not process.is_alive():
            process.join(timeout=0)
            return
        process.terminate()
        terminate_deadline_ns = min(
            deadline_ns, time.monotonic_ns() + _CHECKER_REAP_RESERVE_NS // 2
        )
        remaining_seconds = max(0.0, (terminate_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        process.join(timeout=remaining_seconds)
        if process.is_alive():
            process.kill()
            remaining_seconds = max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
            process.join(timeout=remaining_seconds)
        if process.is_alive() or process.exitcode is None:
            raise R25PostRunIntegrityError(
                "COLLECTOR_CHECKER_TERMINATION_UNCONFIRMED",
                "official Collector checker was not reaped inside its reserved bound",
            )

    try:
        _require_time(checker_deadline_ns)
        remaining = (checker_deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0 or not receive.poll(remaining):
            stop_without_extending_deadline()
            raise R25PostRunIntegrityError(
                "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED",
                "official Collector checker exceeded its absolute deadline",
            )
        try:
            status, payload = receive.recv()
        except EOFError as exc:
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_INTEGRITY_REPORT",
                "official Collector checker exited without a report",
            ) from exc
        remaining = max(0.0, (checker_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        process.join(timeout=remaining)
        if process.is_alive():
            stop_without_extending_deadline()
            raise R25PostRunIntegrityError(
                "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED",
                "official Collector checker did not terminate before deadline",
            )
        if status != "OK":
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_INTEGRITY_REPORT",
                f"official Collector checker failed ({payload})",
            )
        return _object(
            payload,
            code="INVALID_COLLECTOR_INTEGRITY_REPORT",
            name="integrity_report",
        )
    finally:
        receive.close()
        if process.is_alive():
            stop_without_extending_deadline()


def _report_appears_accepted(report: dict[str, JsonValue]) -> bool:
    return (
        report.get("checker_version") == CHECKER_VERSION
        and report.get("valid") is True
        and report.get("errors") == []
        and report.get("warnings") == []
    )


def _run_one_collector_check(
    locator: CollectorRunLocatorV1,
    *,
    repository_root: Path,
    report_root: Path,
    report_root_fd: int,
    deadline_ns: int,
) -> dict[str, JsonValue]:
    _require_time(deadline_ns)
    with _open_stable_owner_directory(
        Path(locator.run_root),
        repository_root=repository_root,
        code="INVALID_COLLECTOR_RUN_ROOT",
    ) as (run_root, run_root_fd, run_metadata):
        _, _, manifest_identity, _ = _validate_collector_run_semantics(
            locator,
            repository_root=repository_root,
            stable_directory_fd=run_root_fd,
            stable_directory_metadata=run_metadata,
        )
        before_run_identity = _path_identity(run_root, run_metadata, kind="DIRECTORY")
        selected_report = _bounded_official_integrity_check(
            run_root_fd,
            collector_run_id=locator.collector_run_id,
            deadline_ns=deadline_ns,
            public_path=None,
        )
        report_raw = canonical_json_bytes(cast(JsonValue, selected_report)) + b"\n"
        report_name = f"pilot-cell-{locator.sequence_index:03d}.integrity.v1.json"
        report_path = report_root / report_name
        report_metadata = _write_fresh_owner_file_at(
            report_root_fd,
            report_name,
            report_raw,
            code="INTEGRITY_REPORT_PUBLICATION_FAILED",
        )
        # Preserve an invalid official report, but never admit it into an
        # acceptance artifact.
        accepted_report = _report_projection(selected_report)
        _require_time(deadline_ns)
        _, _, after_manifest_identity, after_run_metadata = _validate_collector_run_semantics(
            locator,
            repository_root=repository_root,
            stable_directory_fd=run_root_fd,
            stable_directory_metadata=run_metadata,
        )
        after_run_identity = _path_identity(run_root, after_run_metadata, kind="DIRECTORY")
        if (
            before_run_identity != after_run_identity
            or manifest_identity != after_manifest_identity
        ):
            raise R25PostRunIntegrityError(
                "COLLECTOR_EVIDENCE_CHANGED_DURING_CHECK",
                "Collector path identity changed during official validation",
            )
    report_readback, readback_metadata = _read_owner_file_at(
        report_root_fd,
        report_name,
        maximum_bytes=_MAX_INTEGRITY_REPORT_BYTES,
        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
        require_canonical=False,
    )
    if report_readback != report_raw or (readback_metadata.st_dev, readback_metadata.st_ino) != (
        report_metadata.st_dev,
        report_metadata.st_ino,
    ):
        raise R25PostRunIntegrityError(
            "INTEGRITY_REPORT_PUBLICATION_FAILED", "integrity report readback differs"
        )
    return {
        "arm": locator.arm,
        "cleanup_evidence_sha256": locator.cleanup_evidence_sha256,
        "collector_manifest_final": {
            "byte_count": locator.manifest_final_byte_count,
            "path_identity": cast(JsonValue, manifest_identity),
            "sha256": locator.manifest_final_sha256,
        },
        "collector_run_id": locator.collector_run_id,
        "collector_run_root_identity": cast(JsonValue, before_run_identity),
        "collector_task_run_id": locator.task_run_id,
        "host": locator.host,
        "integrity_report": {
            **_file_record(report_path, report_readback, report_metadata),
            "checker_version": cast(str, accepted_report["checker_version"]),
            "error_count": 0,
            "valid": True,
            "warning_count": 0,
        },
        "official_result_sha256": locator.official_result_evidence_sha256,
        "reset_evidence_sha256": locator.reset_evidence_sha256,
        "sequence_index": locator.sequence_index,
        "task_id": locator.task_id,
        "unit_id": locator.unit_id,
        "unit_journal_sha256": locator.unit_journal_sha256,
    }


def _run_one_smoke_collector_check(
    locator: SmokeCollectorRunLocatorV1,
    *,
    repository_root: Path,
    report_root: Path,
    report_root_fd: int,
    deadline_ns: int,
) -> dict[str, JsonValue]:
    _require_time(deadline_ns)
    with _open_stable_owner_directory(
        Path(locator.run_root),
        repository_root=repository_root,
        code="INVALID_SMOKE_COLLECTOR_RUN_ROOT",
    ) as (run_root, run_root_fd, run_metadata):
        _, _, manifest_identity, _ = _validate_smoke_collector_run_semantics(
            locator,
            repository_root=repository_root,
            stable_directory_fd=run_root_fd,
            stable_directory_metadata=run_metadata,
        )
        before_run_identity = _path_identity(run_root, run_metadata, kind="DIRECTORY")
        report = _bounded_official_integrity_check(
            run_root_fd,
            collector_run_id=locator.collector_run_id,
            deadline_ns=deadline_ns,
            public_path=None,
        )
        report_raw = canonical_json_bytes(cast(JsonValue, report)) + b"\n"
        report_name = f"smoke-{locator.sequence_index:02d}-{locator.host.lower()}-{locator.mode.lower()}.integrity.v1.json"
        report_path = report_root / report_name
        report_metadata = _write_fresh_owner_file_at(
            report_root_fd,
            report_name,
            report_raw,
            code="INTEGRITY_REPORT_PUBLICATION_FAILED",
        )
        accepted_report = _report_projection(report)
        _require_time(deadline_ns)
        _, _, after_manifest_identity, after_run_metadata = _validate_smoke_collector_run_semantics(
            locator,
            repository_root=repository_root,
            stable_directory_fd=run_root_fd,
            stable_directory_metadata=run_metadata,
        )
        if (
            before_run_identity != _path_identity(run_root, after_run_metadata, kind="DIRECTORY")
            or manifest_identity != after_manifest_identity
        ):
            raise R25PostRunIntegrityError(
                "COLLECTOR_EVIDENCE_CHANGED_DURING_CHECK",
                "smoke Collector identity changed during official validation",
            )
    report_readback, readback_metadata = _read_owner_file_at(
        report_root_fd,
        report_name,
        maximum_bytes=_MAX_INTEGRITY_REPORT_BYTES,
        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
        require_canonical=False,
    )
    if report_readback != report_raw or (
        readback_metadata.st_dev,
        readback_metadata.st_ino,
    ) != (report_metadata.st_dev, report_metadata.st_ino):
        raise R25PostRunIntegrityError(
            "INTEGRITY_REPORT_PUBLICATION_FAILED", "smoke integrity report readback differs"
        )
    return {
        "case_id": locator.case_id,
        "collector_manifest_final": {
            "byte_count": locator.manifest_final_byte_count,
            "path_identity": cast(JsonValue, manifest_identity),
            "sha256": locator.manifest_final_sha256,
        },
        "collector_run_id": locator.collector_run_id,
        "collector_run_root_identity": cast(JsonValue, before_run_identity),
        "collector_task_run_id": locator.task_run_id,
        "host": locator.host,
        "integrity_report": {
            **_file_record(report_path, report_readback, report_metadata),
            "checker_version": cast(str, accepted_report["checker_version"]),
            "error_count": 0,
            "valid": True,
            "warning_count": 0,
        },
        "mode": locator.mode,
        "sequence_index": locator.sequence_index,
        "stage": locator.stage,
        "task_id": locator.task_id,
        "unit_id": locator.unit_id,
        "unit_journal_sha256": locator.unit_journal_sha256,
    }


def _sequence_file_records(
    evidence: _SequenceEvidenceV1,
) -> tuple[
    dict[str, JsonValue],
    dict[str, JsonValue],
    list[dict[str, JsonValue]],
]:
    terminal = _file_record(
        evidence.output_root / "terminal.json",
        evidence.terminal_raw,
        evidence.terminal_metadata,
    )
    cleanup = _file_record(
        evidence.output_root / "04-resource-cleanup.json",
        evidence.cleanup_raw,
        evidence.cleanup_metadata,
    )
    stages = [
        {
            **_file_record(evidence.output_root / filename, raw, metadata),
            "stage": stage,
        }
        for stage, filename, _, raw, metadata in evidence.stages
    ]
    return terminal, cleanup, stages


def _ordered_collector_root(records: list[dict[str, JsonValue]]) -> str:
    return _sha_json(
        cast(
            JsonValue,
            {
                "domain": "r2.5-ordered-official-collector-integrity",
                "schema_version": POST_RUN_INTEGRITY_SCHEMA_VERSION,
                "value": records,
            },
        )
    )


def _whole_sequence_collector_root(
    *,
    smoke_root: str,
    smoke_count: int,
    pilot_root: str,
    pilot_count: int,
) -> str:
    return _production_hash(
        "r2.5-post-run-whole-sequence-collector-integrity",
        cast(
            JsonValue,
            {
                "pilot_collector_run_count": pilot_count,
                "ordered_pilot_collector_integrity_root_sha256": pilot_root,
                "smoke_collector_run_count": smoke_count,
                "ordered_smoke_collector_integrity_root_sha256": smoke_root,
            },
        ),
    )


def collector_run_locators_from_pilot_evidence_v1(
    pilot_evidence: dict[str, JsonValue],
    *,
    authority: PostRunIntegrityAuthorityV1,
    repository_root: Path,
) -> tuple[CollectorRunLocatorV1, ...]:
    """Rebuild all cell locators solely from the terminal pilot preimage."""

    validate_pilot_stage_durable_evidence_projection_v2(
        pilot_evidence,
        expected_run_manifest=authority.run_manifest,
        expected_manifest_sha256=authority.authority_manifest_sha256,
        expected_resolved_pilot_inputs_sha256=authority.resolved_pilot_inputs_sha256,
        expected_backend_endpoint=authority.backend_endpoint,
        expected_preflight_report_sha256=authority.preflight_report_sha256,
        expected_factory_binding_sha256=authority.factory_binding_sha256,
    )
    cells = _list(pilot_evidence.get("cells"), code="INVALID_PILOT_STAGE", name="pilot.cells")
    if len(cells) != authority.expected_cell_count:
        raise R25PostRunIntegrityError("INVALID_PILOT_CELL_CENSUS", "pilot locator census differs")
    locators: list[CollectorRunLocatorV1] = []
    seen_roots: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_task_run_ids: set[str] = set()
    for index, cell_value in enumerate(cells):
        cell = _object(cell_value, code="INVALID_PILOT_STAGE", name="pilot.cell")
        locator = _object(
            cell.get("collector_run_locator"),
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.collector_run_locator",
        )
        expected_locator_fields = {
            "collector_manifest_capture_complete",
            "collector_manifest_final_byte_count",
            "collector_manifest_final_path",
            "collector_manifest_final_sha256",
            "collector_manifest_runtime_status",
            "collector_run_id",
            "collector_run_root",
            "collector_task_run_id",
            "manifest_sha256",
            "run_id",
            "sequence_index",
            "task_id",
            "unit_id",
            "unit_journal_sha256",
        }
        if set(locator) != expected_locator_fields:
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "cell Collector locator fields differ"
            )
        locator_hash = cell.get("collector_run_locator_sha256")
        if locator_hash != _sha_json(cast(JsonValue, locator)):
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "cell Collector locator hash differs"
            )
        official = _object(
            cell.get("official_result"),
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.official_result",
        )
        decisions = _list(
            cell.get("decisions"),
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.decisions",
        )
        census = _object(
            cell.get("census"),
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.census",
        )
        unit_journal = _object(
            cell.get("unit_journal"),
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.unit_journal",
        )
        reset_sha256 = cell.get("reset_evidence_sha256")
        official_evidence_sha256 = cell.get("official_result_evidence_sha256")
        cleanup_sha256 = cell.get("cleanup_evidence_sha256")
        unit_journal_sha256 = cell.get("unit_journal_sha256")
        reset = _unwrap_production_preimage(
            cell.get("reset_evidence"),
            expected_domain="production-pilot-reset",
            expected_sha256=reset_sha256,
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.reset_evidence",
        )
        official_evidence = _unwrap_production_preimage(
            cell.get("official_result_evidence"),
            expected_domain="production-official-result",
            expected_sha256=official_evidence_sha256,
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.official_result_evidence",
        )
        cleanup = _unwrap_production_preimage(
            cell.get("cleanup_evidence"),
            expected_domain="production-unit-cleanup",
            expected_sha256=cleanup_sha256,
            code="INVALID_COLLECTOR_LOCATOR",
            name="cell.cleanup_evidence",
        )
        journal_reference_value = cell.get("unit_journal_validated_reference")
        journal_reference = (
            None
            if journal_reference_value is None
            else _object(
                journal_reference_value,
                code="INVALID_COLLECTOR_LOCATOR",
                name="cell.unit_journal_validated_reference",
            )
        )
        if (
            type(unit_journal_sha256) is not str
            or _SHA256.fullmatch(unit_journal_sha256) is None
            or unit_journal_sha256 != _sha_json(cast(JsonValue, unit_journal))
            or cell.get("unit_journal_byte_count")
            != len(canonical_json_bytes(cast(JsonValue, unit_journal)))
        ):
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "cell durable preimage hash differs"
            )
        unit_id = f"pilot:{index:03d}"
        cleanup_expected_fields = {
            "cleanup_dispatch_authorized",
            "collector_manifest_sha256",
            "collector_run_locator",
            "collector_run_locator_sha256",
            "deadline_binding",
            "manifest_sha256",
            "task_run_id",
            "teardown_attempted",
            "teardown_result",
            "teardown_result_sha256",
            "unit_id",
            "unit_journal_sha256",
        }
        if (
            cell.get("sequence_index") != index
            or cell.get("manifest_sha256") != authority.authority_manifest_sha256
            or cell.get("run_id") != authority.run_id
            or locator.get("sequence_index") != index
            or locator.get("manifest_sha256") != authority.authority_manifest_sha256
            or locator.get("run_id") != authority.run_id
            or locator.get("collector_manifest_capture_complete") is not True
            or locator.get("collector_manifest_runtime_status") != "completed"
            or locator.get("task_id") != cell.get("task_id")
            or locator.get("unit_id") != unit_id
            or locator.get("unit_journal_sha256") != unit_journal_sha256
            or reset.get("effective_reset_state_sha256") != cell.get("effective_reset_state_sha256")
            or reset_sha256 != cell.get("reset_receipt_sha256")
            or reset.get("reset_seed") != cell.get("reset_seed")
            or reset.get("task_id") != cell.get("task_id")
            or reset.get("task_parameters_sha256") != cell.get("task_parameters_sha256")
            or official_evidence_sha256 != official.get("result_payload_sha256")
            or cleanup_sha256 != cell.get("cleanup_receipt_sha256")
            or set(cleanup) != cleanup_expected_fields
            or cleanup.get("collector_run_locator") != locator
            or cleanup.get("collector_run_locator_sha256") != locator_hash
            or cleanup.get("collector_manifest_sha256")
            != locator.get("collector_manifest_final_sha256")
            or cleanup.get("manifest_sha256") != authority.authority_manifest_sha256
            or cleanup.get("task_run_id") != locator.get("collector_task_run_id")
            or cleanup.get("unit_id") != unit_id
            or cleanup.get("unit_journal_sha256") != unit_journal_sha256
        ):
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_LOCATOR", "cell/locator/preimage binding differs"
            )
        trusted = CollectorRunLocatorV1(
            sequence_index=index,
            manifest_sha256=authority.authority_manifest_sha256,
            run_id=authority.run_id,
            task_id=cast(str, cell["task_id"]),
            host=cast(str, cell["host"]),
            arm=cast(str, cell["arm"]),
            unit_id=unit_id,
            run_root=cast(str, locator["collector_run_root"]),
            collector_run_id=cast(str, locator["collector_run_id"]),
            task_run_id=cast(str, locator["collector_task_run_id"]),
            manifest_final_path=cast(str, locator["collector_manifest_final_path"]),
            manifest_final_sha256=cast(str, locator["collector_manifest_final_sha256"]),
            manifest_final_byte_count=cast(int, locator["collector_manifest_final_byte_count"]),
            unit_journal_sha256=unit_journal_sha256,
            unit_journal=unit_journal,
            unit_journal_validated_reference=journal_reference,
            reset_evidence=reset,
            reset_evidence_sha256=cast(str, reset_sha256),
            official_result=official,
            official_result_evidence=official_evidence,
            official_result_evidence_sha256=cast(str, official_evidence_sha256),
            cleanup_evidence=cleanup,
            cleanup_evidence_sha256=cast(str, cleanup_sha256),
            decisions=decisions,
            census=census,
            backend_endpoint=authority.backend_endpoint,
            resource_topology=cast(str, authority.run_manifest.resource_topology),
        )
        validate_durable_unit_journal_projection_v1(
            unit_journal,
            locator=trusted,
            authority=authority,
            production_audit_root=(
                None
                if authority.production_audit_root is None
                else Path(authority.production_audit_root)
            ),
            repository_root=repository_root,
        )
        for value, seen, name in (
            (trusted.run_root, seen_roots, "Collector run root"),
            (trusted.collector_run_id, seen_run_ids, "Collector run ID"),
            (trusted.task_run_id, seen_task_run_ids, "Collector task run ID"),
        ):
            if value in seen:
                raise R25PostRunIntegrityError("DUPLICATE_COLLECTOR_RUN", f"{name} is not unique")
            seen.add(value)
        locators.append(trusted)
    return tuple(locators)


def run_post_run_integrity_gate_v1(
    *,
    sequence_output_root: Path,
    repository_root: Path,
    authority: PostRunIntegrityAuthorityV1,
    sequence_started_monotonic_ns: int,
    sequence_deadline_monotonic_ns: int,
) -> tuple[dict[str, JsonValue], str, Path]:
    """Run all official checks and publish one fresh acceptance artifact.

    The function deliberately creates no report directory until the complete
    sequence terminal and resource-cleanup proof have been revalidated.
    Invalid checker results are preserved as individual reports, while the
    aggregate acceptance artifact is never created on any failure.
    """

    started_ns = time.monotonic_ns()
    if (
        sequence_output_root != Path(authority.run_manifest.output_root)
        or type(sequence_started_monotonic_ns) is not int
        or sequence_started_monotonic_ns <= 0
        or sequence_started_monotonic_ns > started_ns
        or type(sequence_deadline_monotonic_ns) is not int
        or sequence_deadline_monotonic_ns <= started_ns
        or sequence_deadline_monotonic_ns - started_ns
        > authority.max_wall_time_seconds * 1_000_000_000
        or sequence_deadline_monotonic_ns - sequence_started_monotonic_ns
        > authority.max_sequence_wall_time_seconds * 1_000_000_000
    ):
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_DEADLINE",
            "gate requires the pre-reserved remaining sequence deadline",
        )
    deadline_ns = sequence_deadline_monotonic_ns
    with _open_stable_owner_directory(
        sequence_output_root,
        repository_root=repository_root,
        code="INVALID_SEQUENCE_OUTPUT_ROOT",
    ) as (stable_sequence_root, sequence_root_fd, sequence_root_metadata):
        evidence = _load_completed_sequence(
            stable_sequence_root,
            repository_root=repository_root,
            authority=authority,
            stable_directory_fd=sequence_root_fd,
            stable_directory_metadata=sequence_root_metadata,
        )
        _require_time(deadline_ns)
        pilot_locators = collector_run_locators_from_pilot_evidence_v1(
            evidence.pilot_evidence,
            authority=authority,
            repository_root=repository_root,
        )
        smoke_locators = smoke_collector_run_locators_from_stage_evidence_v2(
            evidence.smoke_evidence,
            authority=authority,
        )
        if len(pilot_locators) != authority.expected_cell_count or len(smoke_locators) != 6:
            raise R25PostRunIntegrityError(
                "INVALID_COLLECTOR_RUN_CENSUS", "smoke/pilot Collector locator count differs"
            )
        all_raw_roots = [item.run_root for item in smoke_locators] + [
            item.run_root for item in pilot_locators
        ]
        all_run_ids = [item.collector_run_id for item in smoke_locators] + [
            item.collector_run_id for item in pilot_locators
        ]
        all_task_run_ids = [item.task_run_id for item in smoke_locators] + [
            item.task_run_id for item in pilot_locators
        ]
        if any(
            len(values) != len(set(values))
            for values in (all_raw_roots, all_run_ids, all_task_run_ids)
        ):
            raise R25PostRunIntegrityError(
                "DUPLICATE_COLLECTOR_RUN", "smoke and pilot Collector identities overlap"
            )
        audit_root_identity_sha256 = _authority_audit_root_identity(
            authority, repository_root=repository_root
        )
        with _create_fresh_report_root(
            stable_sequence_root,
            repository_root=repository_root,
        ) as (reports, report_root_fd, report_root_metadata):
            smoke_records: list[dict[str, JsonValue]] = []
            for smoke_locator in smoke_locators:
                smoke_records.append(
                    _run_one_smoke_collector_check(
                        smoke_locator,
                        repository_root=repository_root,
                        report_root=reports,
                        report_root_fd=report_root_fd,
                        deadline_ns=deadline_ns,
                    )
                )
            pilot_records: list[dict[str, JsonValue]] = []
            for pilot_locator in pilot_locators:
                pilot_records.append(
                    _run_one_collector_check(
                        pilot_locator,
                        repository_root=repository_root,
                        report_root=reports,
                        report_root_fd=report_root_fd,
                        deadline_ns=deadline_ns,
                    )
                )
            raw_identity_hashes = [
                _object(
                    record["collector_run_root_identity"],
                    code="INVALID_COLLECTOR_RUN_ROOT",
                    name="collector_run_root_identity",
                ).get("identity_sha256")
                for record in smoke_records + pilot_records
            ]
            if (
                [record["sequence_index"] for record in smoke_records] != list(range(6))
                or [record["sequence_index"] for record in pilot_records]
                != list(range(authority.expected_cell_count))
                or len(set(raw_identity_hashes)) != authority.expected_cell_count + 6
            ):
                raise R25PostRunIntegrityError(
                    "INVALID_COLLECTOR_RUN_CENSUS",
                    "Collector report order/root identity differs",
                )
            _require_time(deadline_ns)
            final_sequence = _load_completed_sequence(
                stable_sequence_root,
                repository_root=repository_root,
                authority=authority,
                stable_directory_fd=sequence_root_fd,
                stable_directory_metadata=sequence_root_metadata,
            )
            if (
                final_sequence.output_root_identity != evidence.output_root_identity
                or final_sequence.binding_raw != evidence.binding_raw
                or final_sequence.terminal_raw != evidence.terminal_raw
                or final_sequence.cleanup_raw != evidence.cleanup_raw
                or tuple(item[3] for item in final_sequence.stages)
                != tuple(item[3] for item in evidence.stages)
            ):
                raise R25PostRunIntegrityError(
                    "SEQUENCE_EVIDENCE_CHANGED_DURING_CHECK",
                    "sequence terminal evidence changed during Collector validation",
                )
            smoke_root = _ordered_collector_root(smoke_records)
            pilot_root = _ordered_collector_root(pilot_records)
            whole_root = _whole_sequence_collector_root(
                smoke_root=smoke_root,
                smoke_count=len(smoke_records),
                pilot_root=pilot_root,
                pilot_count=len(pilot_records),
            )
            terminal_file, cleanup_file, stage_files = _sequence_file_records(evidence)
            binding_raw, binding_metadata = _read_owner_file_at(
                sequence_root_fd,
                "manifest-binding.json",
                maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
                code="INVALID_SEQUENCE_BINDING",
            )
            if binding_raw != evidence.binding_raw:
                raise R25PostRunIntegrityError(
                    "SEQUENCE_BINDING_MISMATCH", "manifest binding changed during gate"
                )
            elapsed_ms = (time.monotonic_ns() - started_ns + 999_999) // 1_000_000
            total_sequence_wall_time_ms = (
                time.monotonic_ns() - sequence_started_monotonic_ns + 999_999
            ) // 1_000_000
            if (
                elapsed_ms > authority.max_wall_time_seconds * 1000
                or total_sequence_wall_time_ms > authority.max_sequence_wall_time_seconds * 1000
            ):
                raise R25PostRunIntegrityError(
                    "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED", "gate wall authority elapsed"
                )
            artifact: dict[str, JsonValue] = {
                "authority_manifest_sha256": authority.authority_manifest_sha256,
                "backend_endpoint": authority.backend_endpoint,
                "factory_binding_sha256": authority.factory_binding_sha256,
                "pilot_manifest": cast(
                    JsonValue, frozen_pilot_manifest_projection(authority.pilot_manifest)
                ),
                "pilot_manifest_sha256": authority.pilot_manifest_sha256,
                "preflight_report_sha256": authority.preflight_report_sha256,
                "pricing_sha256": authority.pricing_sha256,
                "production_audit_root_identity_sha256": audit_root_identity_sha256,
                "resolved_pilot_inputs_sha256": authority.resolved_pilot_inputs_sha256,
                "runtime_config_sha256": authority.runtime_config_sha256,
                "sentinel_config_sha256": authority.sentinel_config_sha256,
                "checker_version": CHECKER_VERSION,
                "cleanup_file": cast(JsonValue, cleanup_file),
                "collector_run_count": len(smoke_records) + len(pilot_records),
                "pilot_collector_run_count": len(pilot_records),
                "pilot_collector_runs": cast(JsonValue, pilot_records),
                "smoke_collector_run_count": len(smoke_records),
                "smoke_collector_runs": cast(JsonValue, smoke_records),
                "manifest_binding_file": cast(
                    JsonValue,
                    _file_record(
                        evidence.output_root / "manifest-binding.json",
                        binding_raw,
                        binding_metadata,
                    ),
                ),
                "max_post_run_integrity_wall_time_seconds": authority.max_wall_time_seconds,
                "max_sequence_wall_time_seconds": authority.max_sequence_wall_time_seconds,
                "ordered_collector_integrity_root_sha256": whole_root,
                "ordered_pilot_collector_integrity_root_sha256": pilot_root,
                "ordered_smoke_collector_integrity_root_sha256": smoke_root,
                "post_run_integrity_wall_time_ms": elapsed_ms,
                "report_root_identity": cast(
                    JsonValue,
                    _path_identity(reports, report_root_metadata, kind="DIRECTORY"),
                ),
                "run_id": authority.run_id,
                "schema_version": POST_RUN_INTEGRITY_SCHEMA_VERSION,
                "sequence_output_root_identity": cast(JsonValue, evidence.output_root_identity),
                "source_commit": evidence.source_commit,
                "stage_files": cast(JsonValue, stage_files),
                "status": "VALID",
                "terminal_file": cast(JsonValue, terminal_file),
                "total_sequence_wall_time_ms": total_sequence_wall_time_ms,
                "warnings_policy": POST_RUN_INTEGRITY_WARNINGS_POLICY,
            }
            artifact_raw = canonical_json_bytes(cast(JsonValue, artifact))
            artifact_name = _ACCEPTANCE_ARTIFACT_NAME
            artifact_path = reports / artifact_name
            artifact_metadata = _write_fresh_owner_file_at(
                report_root_fd,
                artifact_name,
                artifact_raw,
                code="INTEGRITY_ACCEPTANCE_PUBLICATION_FAILED",
            )
            readback, readback_metadata = _read_owner_file_at(
                report_root_fd,
                artifact_name,
                maximum_bytes=_MAX_ACCEPTANCE_ARTIFACT_BYTES,
                code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
            )
            if readback != artifact_raw or (readback_metadata.st_dev, readback_metadata.st_ino) != (
                artifact_metadata.st_dev,
                artifact_metadata.st_ino,
            ):
                raise R25PostRunIntegrityError(
                    "INTEGRITY_ACCEPTANCE_PUBLICATION_FAILED", "acceptance readback differs"
                )
            completion_elapsed_ms = (time.monotonic_ns() - started_ns + 999_999) // 1_000_000
            completion_total_sequence_ms = (
                time.monotonic_ns() - sequence_started_monotonic_ns + 999_999
            ) // 1_000_000
            if (
                completion_elapsed_ms > authority.max_wall_time_seconds * 1000
                or completion_total_sequence_ms > authority.max_sequence_wall_time_seconds * 1000
            ):
                raise R25PostRunIntegrityError(
                    "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED",
                    "acceptance fsync exceeded wall authority",
                )
            completion: dict[str, JsonValue] = {
                "acceptance_artifact": cast(
                    JsonValue,
                    _file_record(artifact_path, readback, readback_metadata),
                ),
                "authority_manifest_sha256": authority.authority_manifest_sha256,
                "backend_endpoint": authority.backend_endpoint,
                "completed_through_acceptance_fsync": True,
                "max_post_run_integrity_wall_time_seconds": authority.max_wall_time_seconds,
                "max_sequence_wall_time_seconds": authority.max_sequence_wall_time_seconds,
                "ordered_collector_integrity_root_sha256": artifact[
                    "ordered_collector_integrity_root_sha256"
                ],
                "ordered_pilot_collector_integrity_root_sha256": artifact[
                    "ordered_pilot_collector_integrity_root_sha256"
                ],
                "ordered_smoke_collector_integrity_root_sha256": artifact[
                    "ordered_smoke_collector_integrity_root_sha256"
                ],
                "pilot_collector_run_count": len(pilot_records),
                "smoke_collector_run_count": len(smoke_records),
                "collector_run_count": len(smoke_records) + len(pilot_records),
                "pilot_manifest_sha256": authority.pilot_manifest_sha256,
                "post_run_integrity_wall_time_ms_through_acceptance_fsync": (completion_elapsed_ms),
                "report_root_identity": cast(
                    JsonValue,
                    _path_identity(reports, report_root_metadata, kind="DIRECTORY"),
                ),
                "resolved_pilot_inputs_sha256": authority.resolved_pilot_inputs_sha256,
                "run_id": authority.run_id,
                "schema_version": POST_RUN_INTEGRITY_COMPLETION_SCHEMA_VERSION,
                "status": "COMPLETE",
                "total_sequence_wall_time_ms_through_acceptance_fsync": (
                    completion_total_sequence_ms
                ),
            }
            completion_raw = canonical_json_bytes(cast(JsonValue, completion))
            completion_metadata = _write_fresh_owner_file_at(
                report_root_fd,
                _ACCEPTANCE_COMPLETION_NAME,
                completion_raw,
                code="INTEGRITY_ACCEPTANCE_PUBLICATION_FAILED",
            )
            completion_readback, completion_readback_metadata = _read_owner_file_at(
                report_root_fd,
                _ACCEPTANCE_COMPLETION_NAME,
                maximum_bytes=_MAX_ACCEPTANCE_ARTIFACT_BYTES,
                code="INVALID_POST_RUN_INTEGRITY_COMPLETION",
            )
            if completion_readback != completion_raw or (
                completion_readback_metadata.st_dev,
                completion_readback_metadata.st_ino,
            ) != (completion_metadata.st_dev, completion_metadata.st_ino):
                raise R25PostRunIntegrityError(
                    "INTEGRITY_ACCEPTANCE_PUBLICATION_FAILED",
                    "completion readback differs",
                )
            _require_time(deadline_ns)
    return artifact, _sha_bytes(artifact_raw), artifact_path


def _require_artifact_projection(
    artifact: dict[str, JsonValue], *, authority: PostRunIntegrityAuthorityV1
) -> None:
    expected_fields = {
        "authority_manifest_sha256",
        "backend_endpoint",
        "factory_binding_sha256",
        "pilot_manifest",
        "pilot_manifest_sha256",
        "preflight_report_sha256",
        "pricing_sha256",
        "production_audit_root_identity_sha256",
        "resolved_pilot_inputs_sha256",
        "runtime_config_sha256",
        "sentinel_config_sha256",
        "checker_version",
        "cleanup_file",
        "collector_run_count",
        "pilot_collector_run_count",
        "pilot_collector_runs",
        "smoke_collector_run_count",
        "smoke_collector_runs",
        "manifest_binding_file",
        "max_post_run_integrity_wall_time_seconds",
        "max_sequence_wall_time_seconds",
        "ordered_collector_integrity_root_sha256",
        "ordered_pilot_collector_integrity_root_sha256",
        "ordered_smoke_collector_integrity_root_sha256",
        "post_run_integrity_wall_time_ms",
        "report_root_identity",
        "run_id",
        "schema_version",
        "sequence_output_root_identity",
        "source_commit",
        "stage_files",
        "status",
        "terminal_file",
        "total_sequence_wall_time_ms",
        "warnings_policy",
    }
    if set(artifact) != expected_fields:
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_ARTIFACT", "acceptance fields differ"
        )
    if (
        artifact.get("authority_manifest_sha256") != authority.authority_manifest_sha256
        or artifact.get("backend_endpoint") != authority.backend_endpoint
        or artifact.get("factory_binding_sha256") != authority.factory_binding_sha256
        or artifact.get("pilot_manifest")
        != frozen_pilot_manifest_projection(authority.pilot_manifest)
        or artifact.get("pilot_manifest_sha256") != authority.pilot_manifest_sha256
        or artifact.get("preflight_report_sha256") != authority.preflight_report_sha256
        or artifact.get("pricing_sha256") != authority.pricing_sha256
        or artifact.get("resolved_pilot_inputs_sha256") != authority.resolved_pilot_inputs_sha256
        or artifact.get("runtime_config_sha256") != authority.runtime_config_sha256
        or artifact.get("sentinel_config_sha256") != authority.sentinel_config_sha256
        or artifact.get("run_id") != authority.run_id
        or artifact.get("source_commit") != authority.source_commit
    ):
        raise R25PostRunIntegrityError(
            "POST_RUN_INTEGRITY_BINDING_MISMATCH", "acceptance authority roots differ"
        )
    pilot_records = _list(
        artifact.get("pilot_collector_runs"),
        code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
        name="pilot_collector_runs",
    )
    smoke_records = _list(
        artifact.get("smoke_collector_runs"),
        code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
        name="smoke_collector_runs",
    )
    stages = _list(
        artifact.get("stage_files"),
        code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
        name="stage_files",
    )
    if (
        artifact.get("schema_version") != POST_RUN_INTEGRITY_SCHEMA_VERSION
        or artifact.get("status") != "VALID"
        or artifact.get("checker_version") != CHECKER_VERSION
        or artifact.get("warnings_policy") != POST_RUN_INTEGRITY_WARNINGS_POLICY
        or artifact.get("pilot_collector_run_count") != authority.expected_cell_count
        or artifact.get("smoke_collector_run_count") != 6
        or artifact.get("collector_run_count") != authority.expected_cell_count + 6
        or len(pilot_records) != authority.expected_cell_count
        or len(smoke_records) != 6
        or len(stages) != 4
        or artifact.get("max_post_run_integrity_wall_time_seconds")
        != authority.max_wall_time_seconds
        or artifact.get("max_sequence_wall_time_seconds")
        != authority.max_sequence_wall_time_seconds
        or type(artifact.get("post_run_integrity_wall_time_ms")) is not int
        or not 0
        <= cast(int, artifact["post_run_integrity_wall_time_ms"])
        <= authority.max_wall_time_seconds * 1000
        or type(artifact.get("total_sequence_wall_time_ms")) is not int
        or not 0
        <= cast(int, artifact["total_sequence_wall_time_ms"])
        <= authority.max_sequence_wall_time_seconds * 1000
        or artifact.get("ordered_pilot_collector_integrity_root_sha256")
        != _ordered_collector_root(cast(list[dict[str, JsonValue]], pilot_records))
        or artifact.get("ordered_smoke_collector_integrity_root_sha256")
        != _ordered_collector_root(cast(list[dict[str, JsonValue]], smoke_records))
        or artifact.get("ordered_collector_integrity_root_sha256")
        != _whole_sequence_collector_root(
            smoke_root=cast(str, artifact["ordered_smoke_collector_integrity_root_sha256"]),
            smoke_count=6,
            pilot_root=cast(str, artifact["ordered_pilot_collector_integrity_root_sha256"]),
            pilot_count=authority.expected_cell_count,
        )
        or type(artifact.get("source_commit")) is not str
        or re.fullmatch(r"[0-9a-f]{40}", cast(str, artifact["source_commit"])) is None
        or (
            authority.production_audit_root is None
            and artifact.get("production_audit_root_identity_sha256") is not None
        )
        or (
            authority.production_audit_root is not None
            and (
                type(artifact.get("production_audit_root_identity_sha256")) is not str
                or _SHA256.fullmatch(cast(str, artifact["production_audit_root_identity_sha256"]))
                is None
            )
        )
    ):
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_ARTIFACT", "acceptance invariants differ"
        )


def _require_completion_projection(
    completion: dict[str, JsonValue],
    *,
    artifact: dict[str, JsonValue],
    artifact_path: Path,
    artifact_raw: bytes,
    artifact_metadata: os.stat_result,
    report_root: Path,
    report_root_metadata: os.stat_result,
    authority: PostRunIntegrityAuthorityV1,
) -> None:
    expected_fields = {
        "acceptance_artifact",
        "authority_manifest_sha256",
        "backend_endpoint",
        "completed_through_acceptance_fsync",
        "max_post_run_integrity_wall_time_seconds",
        "max_sequence_wall_time_seconds",
        "ordered_collector_integrity_root_sha256",
        "ordered_pilot_collector_integrity_root_sha256",
        "ordered_smoke_collector_integrity_root_sha256",
        "collector_run_count",
        "pilot_collector_run_count",
        "smoke_collector_run_count",
        "pilot_manifest_sha256",
        "post_run_integrity_wall_time_ms_through_acceptance_fsync",
        "report_root_identity",
        "resolved_pilot_inputs_sha256",
        "run_id",
        "schema_version",
        "status",
        "total_sequence_wall_time_ms_through_acceptance_fsync",
    }
    artifact_record = _file_record(artifact_path, artifact_raw, artifact_metadata)
    report_identity = _path_identity(report_root, report_root_metadata, kind="DIRECTORY")
    elapsed = completion.get("post_run_integrity_wall_time_ms_through_acceptance_fsync")
    total = completion.get("total_sequence_wall_time_ms_through_acceptance_fsync")
    if (
        set(completion) != expected_fields
        or completion.get("schema_version") != POST_RUN_INTEGRITY_COMPLETION_SCHEMA_VERSION
        or completion.get("status") != "COMPLETE"
        or completion.get("completed_through_acceptance_fsync") is not True
        or completion.get("acceptance_artifact") != artifact_record
        or completion.get("report_root_identity") != report_identity
        or completion.get("authority_manifest_sha256") != authority.authority_manifest_sha256
        or completion.get("pilot_manifest_sha256") != authority.pilot_manifest_sha256
        or completion.get("resolved_pilot_inputs_sha256") != authority.resolved_pilot_inputs_sha256
        or completion.get("backend_endpoint") != authority.backend_endpoint
        or completion.get("run_id") != authority.run_id
        or completion.get("max_post_run_integrity_wall_time_seconds")
        != authority.max_wall_time_seconds
        or completion.get("max_sequence_wall_time_seconds")
        != authority.max_sequence_wall_time_seconds
        or completion.get("ordered_collector_integrity_root_sha256")
        != artifact.get("ordered_collector_integrity_root_sha256")
        or completion.get("ordered_pilot_collector_integrity_root_sha256")
        != artifact.get("ordered_pilot_collector_integrity_root_sha256")
        or completion.get("ordered_smoke_collector_integrity_root_sha256")
        != artifact.get("ordered_smoke_collector_integrity_root_sha256")
        or completion.get("pilot_collector_run_count") != authority.expected_cell_count
        or completion.get("smoke_collector_run_count") != 6
        or completion.get("collector_run_count") != authority.expected_cell_count + 6
        or type(elapsed) is not int
        or not 0 <= elapsed <= authority.max_wall_time_seconds * 1000
        or type(total) is not int
        or not 0 <= total <= authority.max_sequence_wall_time_seconds * 1000
        or elapsed < cast(int, artifact["post_run_integrity_wall_time_ms"])
        or total < cast(int, artifact["total_sequence_wall_time_ms"])
    ):
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_COMPLETION",
            "durable completion marker differs",
        )


def _identity_path(value: object, *, name: str) -> Path:
    identity = _object(
        value, code="INVALID_POST_RUN_INTEGRITY_ARTIFACT", name=f"{name}.path_identity"
    )
    path_value = identity.get("canonical_path")
    if type(path_value) is not str or not Path(path_value).is_absolute():
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_ARTIFACT", f"{name} path is invalid"
        )
    return Path(path_value)


def _reopen_file_record(
    value: object,
    *,
    maximum_bytes: int,
    code: str,
    require_canonical: bool,
) -> tuple[Path, bytes, os.stat_result]:
    record = _object(value, code=code, name="file_record")
    if set(record) != {"byte_count", "path_identity", "sha256"}:
        raise R25PostRunIntegrityError(code, "file record fields differ")
    path = _identity_path(record.get("path_identity"), name="file_record")
    raw, metadata = _read_owner_file(
        path,
        maximum_bytes=maximum_bytes,
        code=code,
        require_canonical=require_canonical,
    )
    if (
        record.get("byte_count") != len(raw)
        or record.get("sha256") != _sha_bytes(raw)
        or record.get("path_identity") != _path_identity(path, metadata, kind="REGULAR_FILE")
    ):
        raise R25PostRunIntegrityError(code, "file record content/identity differs")
    return path, raw, metadata


def _reopen_file_record_at(
    value: object,
    *,
    directory_fd: int,
    expected_path: Path,
    maximum_bytes: int,
    code: str,
    require_canonical: bool,
) -> tuple[bytes, os.stat_result]:
    record = _object(value, code=code, name="file_record")
    if set(record) != {"byte_count", "path_identity", "sha256"}:
        raise R25PostRunIntegrityError(code, "file record fields differ")
    if _identity_path(record.get("path_identity"), name="file_record") != expected_path:
        raise R25PostRunIntegrityError(code, "file record path differs")
    raw, metadata = _read_owner_file_at(
        directory_fd,
        expected_path.name,
        maximum_bytes=maximum_bytes,
        code=code,
        require_canonical=require_canonical,
    )
    if (
        record.get("byte_count") != len(raw)
        or record.get("sha256") != _sha_bytes(raw)
        or record.get("path_identity")
        != _path_identity(expected_path, metadata, kind="REGULAR_FILE")
    ):
        raise R25PostRunIntegrityError(code, "file record content/identity differs")
    return raw, metadata


def reopen_validate_post_run_integrity_artifact_v1(
    artifact_path: Path,
    *,
    repository_root: Path,
    authority: PostRunIntegrityAuthorityV1,
    rerun_official_checker: bool = True,
) -> tuple[dict[str, JsonValue], str]:
    """Strictly reopen an acceptance artifact and all of its bound evidence."""

    reopen_deadline_ns = time.monotonic_ns() + authority.max_wall_time_seconds * 1_000_000_000
    if (
        not artifact_path.is_absolute()
        or artifact_path.name != _ACCEPTANCE_ARTIFACT_NAME
        or ".." in artifact_path.parts
    ):
        raise R25PostRunIntegrityError(
            "INVALID_POST_RUN_INTEGRITY_ARTIFACT", "acceptance path differs"
        )
    with _open_stable_owner_directory(
        artifact_path.parent,
        repository_root=repository_root,
        code="INVALID_INTEGRITY_REPORT_ROOT",
    ) as (report_root, report_root_fd, report_root_metadata):
        raw, artifact_metadata = _read_owner_file_at(
            report_root_fd,
            _ACCEPTANCE_ARTIFACT_NAME,
            maximum_bytes=_MAX_ACCEPTANCE_ARTIFACT_BYTES,
            code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
        )
        artifact = _decode_canonical_json(raw, code="INVALID_POST_RUN_INTEGRITY_ARTIFACT")
        _require_artifact_projection(artifact, authority=authority)
        report_root_value = _object(
            artifact.get("report_root_identity"),
            code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
            name="report_root_identity",
        )
        if (
            artifact_path != report_root / _ACCEPTANCE_ARTIFACT_NAME
            or report_root_value
            != _path_identity(report_root, report_root_metadata, kind="DIRECTORY")
        ):
            raise R25PostRunIntegrityError(
                "INVALID_POST_RUN_INTEGRITY_ARTIFACT", "report root binding differs"
            )
        expected_report_names = {
            _ACCEPTANCE_ARTIFACT_NAME,
            _ACCEPTANCE_COMPLETION_NAME,
            *(
                f"smoke-{index:02d}-{host.lower()}-{mode.lower()}.integrity.v1.json"
                for index, host, mode in (
                    (0, "QWEN3_VL", "OFF"),
                    (1, "QWEN3_VL", "SHADOW"),
                    (2, "QWEN3_VL", "ACTIVE"),
                    (3, "MAI_UI", "OFF"),
                    (4, "MAI_UI", "SHADOW"),
                    (5, "MAI_UI", "ACTIVE"),
                )
            ),
            *(
                f"pilot-cell-{index:03d}.integrity.v1.json"
                for index in range(authority.expected_cell_count)
            ),
        }
        if set(os.listdir(report_root_fd)) != expected_report_names:
            raise R25PostRunIntegrityError(
                "INVALID_INTEGRITY_REPORT_CENSUS", "integrity report file census differs"
            )
        completion_raw, _ = _read_owner_file_at(
            report_root_fd,
            _ACCEPTANCE_COMPLETION_NAME,
            maximum_bytes=_MAX_ACCEPTANCE_ARTIFACT_BYTES,
            code="INVALID_POST_RUN_INTEGRITY_COMPLETION",
        )
        completion = _decode_canonical_json(
            completion_raw, code="INVALID_POST_RUN_INTEGRITY_COMPLETION"
        )
        _require_completion_projection(
            completion,
            artifact=artifact,
            artifact_path=artifact_path,
            artifact_raw=raw,
            artifact_metadata=artifact_metadata,
            report_root=report_root,
            report_root_metadata=report_root_metadata,
            authority=authority,
        )
        current_audit_identity = _authority_audit_root_identity(
            authority, repository_root=repository_root
        )
        if artifact.get("production_audit_root_identity_sha256") != current_audit_identity:
            raise R25PostRunIntegrityError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH", "production audit root differs"
            )

        sequence_identity = _object(
            artifact.get("sequence_output_root_identity"),
            code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
            name="sequence_output_root_identity",
        )
        sequence_root = _identity_path(sequence_identity, name="sequence_output_root")
        if derived_post_run_integrity_root_v1(sequence_root) != report_root:
            raise R25PostRunIntegrityError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH", "derived report root differs"
            )
        with _open_stable_owner_directory(
            sequence_root,
            repository_root=repository_root,
            code="INVALID_SEQUENCE_OUTPUT_ROOT",
        ) as (stable_sequence_root, sequence_root_fd, sequence_root_metadata):
            sequence = _load_completed_sequence(
                stable_sequence_root,
                repository_root=repository_root,
                authority=authority,
                stable_directory_fd=sequence_root_fd,
                stable_directory_metadata=sequence_root_metadata,
            )
            if sequence.output_root_identity != sequence_identity:
                raise R25PostRunIntegrityError(
                    "POST_RUN_INTEGRITY_BINDING_MISMATCH", "sequence root identity differs"
                )
            manifest_raw, _ = _reopen_file_record_at(
                artifact.get("manifest_binding_file"),
                directory_fd=sequence_root_fd,
                expected_path=stable_sequence_root / "manifest-binding.json",
                maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
                code="INVALID_SEQUENCE_BINDING",
                require_canonical=True,
            )
            terminal_raw, _ = _reopen_file_record_at(
                artifact.get("terminal_file"),
                directory_fd=sequence_root_fd,
                expected_path=stable_sequence_root / "terminal.json",
                maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
                code="INVALID_SEQUENCE_TERMINAL",
                require_canonical=True,
            )
            cleanup_raw, _ = _reopen_file_record_at(
                artifact.get("cleanup_file"),
                directory_fd=sequence_root_fd,
                expected_path=stable_sequence_root / "04-resource-cleanup.json",
                maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
                code="INVALID_RESOURCE_CLEANUP",
                require_canonical=True,
            )
            if (
                manifest_raw != sequence.binding_raw
                or terminal_raw != sequence.terminal_raw
                or cleanup_raw != sequence.cleanup_raw
            ):
                raise R25PostRunIntegrityError(
                    "POST_RUN_INTEGRITY_BINDING_MISMATCH",
                    "sequence terminal files differ",
                )
            stage_records = _list(
                artifact.get("stage_files"),
                code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
                name="stage_files",
            )
            for expected, record_value in zip(sequence.stages, stage_records, strict=True):
                stage, filename, _, expected_raw, _ = expected
                record = _object(
                    record_value,
                    code="INVALID_STAGE_ARTIFACT",
                    name="stage_file_record",
                )
                stage_file = dict(record)
                if stage_file.pop("stage", None) != stage:
                    raise R25PostRunIntegrityError(
                        "INVALID_STAGE_ARTIFACT", "stage file order differs"
                    )
                stage_raw, _ = _reopen_file_record_at(
                    cast(JsonValue, stage_file),
                    directory_fd=sequence_root_fd,
                    expected_path=stable_sequence_root / filename,
                    maximum_bytes=_MAX_SEQUENCE_DOCUMENT_BYTES,
                    code="INVALID_STAGE_ARTIFACT",
                    require_canonical=True,
                )
                if stage_raw != expected_raw:
                    raise R25PostRunIntegrityError(
                        "INVALID_STAGE_ARTIFACT", "stage file binding differs"
                    )

            smoke_locators = smoke_collector_run_locators_from_stage_evidence_v2(
                sequence.smoke_evidence,
                authority=authority,
            )
            stored_smoke_records = _list(
                artifact.get("smoke_collector_runs"),
                code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
                name="smoke_collector_runs",
            )
            for smoke_locator, stored_value in zip(
                smoke_locators, stored_smoke_records, strict=True
            ):
                stored = _object(
                    stored_value,
                    code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
                    name="smoke_collector_run",
                )
                with _open_stable_owner_directory(
                    Path(smoke_locator.run_root),
                    repository_root=repository_root,
                    code="INVALID_SMOKE_COLLECTOR_RUN_ROOT",
                ) as (run_root, run_root_fd, run_metadata):
                    _, _, manifest_identity, _ = _validate_smoke_collector_run_semantics(
                        smoke_locator,
                        repository_root=repository_root,
                        stable_directory_fd=run_root_fd,
                        stable_directory_metadata=run_metadata,
                    )
                    report_value = _object(
                        stored.get("integrity_report"),
                        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
                        name="smoke_collector_run.integrity_report",
                    )
                    report_file = dict(report_value)
                    for key, expected_value in (
                        ("checker_version", CHECKER_VERSION),
                        ("error_count", 0),
                        ("valid", True),
                        ("warning_count", 0),
                    ):
                        if report_file.pop(key, None) != expected_value:
                            raise R25PostRunIntegrityError(
                                "INVALID_COLLECTOR_INTEGRITY_REPORT",
                                "smoke report summary differs",
                            )
                    report_name = f"smoke-{smoke_locator.sequence_index:02d}-{smoke_locator.host.lower()}-{smoke_locator.mode.lower()}.integrity.v1.json"
                    report_raw, _ = _reopen_file_record_at(
                        cast(JsonValue, report_file),
                        directory_fd=report_root_fd,
                        expected_path=report_root / report_name,
                        maximum_bytes=_MAX_INTEGRITY_REPORT_BYTES,
                        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
                        require_canonical=False,
                    )
                    report_document = _decode_canonical_json_line(
                        report_raw, code="INVALID_COLLECTOR_INTEGRITY_REPORT"
                    )
                    _report_projection(report_document)
                    expected_record: dict[str, JsonValue] = {
                        "case_id": smoke_locator.case_id,
                        "collector_manifest_final": {
                            "byte_count": smoke_locator.manifest_final_byte_count,
                            "path_identity": cast(JsonValue, manifest_identity),
                            "sha256": smoke_locator.manifest_final_sha256,
                        },
                        "collector_run_id": smoke_locator.collector_run_id,
                        "collector_run_root_identity": cast(
                            JsonValue,
                            _path_identity(run_root, run_metadata, kind="DIRECTORY"),
                        ),
                        "collector_task_run_id": smoke_locator.task_run_id,
                        "host": smoke_locator.host,
                        "integrity_report": report_value,
                        "mode": smoke_locator.mode,
                        "sequence_index": smoke_locator.sequence_index,
                        "stage": smoke_locator.stage,
                        "task_id": smoke_locator.task_id,
                        "unit_id": smoke_locator.unit_id,
                        "unit_journal_sha256": smoke_locator.unit_journal_sha256,
                    }
                    if stored != expected_record:
                        raise R25PostRunIntegrityError(
                            "POST_RUN_INTEGRITY_BINDING_MISMATCH",
                            "smoke Collector acceptance record differs",
                        )
                    if rerun_official_checker:
                        checked = _report_projection(
                            _bounded_official_integrity_check(
                                run_root_fd,
                                collector_run_id=smoke_locator.collector_run_id,
                                deadline_ns=reopen_deadline_ns,
                                public_path=None,
                            )
                        )
                        for key in (
                            "checker_version",
                            "counts",
                            "errors",
                            "valid",
                            "warnings",
                        ):
                            if checked.get(key) != report_document.get(key):
                                raise R25PostRunIntegrityError(
                                    "COLLECTOR_INTEGRITY_RECHECK_MISMATCH",
                                    "smoke official recheck differs from durable report",
                                )

            locators = collector_run_locators_from_pilot_evidence_v1(
                sequence.pilot_evidence,
                authority=authority,
                repository_root=repository_root,
            )
            all_raw_roots = [item.run_root for item in smoke_locators] + [
                item.run_root for item in locators
            ]
            all_run_ids = [item.collector_run_id for item in smoke_locators] + [
                item.collector_run_id for item in locators
            ]
            all_task_run_ids = [item.task_run_id for item in smoke_locators] + [
                item.task_run_id for item in locators
            ]
            if any(
                len(values) != len(set(values))
                for values in (all_raw_roots, all_run_ids, all_task_run_ids)
            ):
                raise R25PostRunIntegrityError(
                    "DUPLICATE_COLLECTOR_RUN",
                    "smoke and pilot Collector identities overlap on reopen",
                )
            stored_records = _list(
                artifact.get("pilot_collector_runs"),
                code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
                name="pilot_collector_runs",
            )
            for locator, stored_value in zip(locators, stored_records, strict=True):
                stored = _object(
                    stored_value,
                    code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
                    name="collector_run",
                )
                with _open_stable_owner_directory(
                    Path(locator.run_root),
                    repository_root=repository_root,
                    code="INVALID_COLLECTOR_RUN_ROOT",
                ) as (run_root, run_root_fd, run_metadata):
                    _, _, manifest_identity, _ = _validate_collector_run_semantics(
                        locator,
                        repository_root=repository_root,
                        stable_directory_fd=run_root_fd,
                        stable_directory_metadata=run_metadata,
                    )
                    report_value = _object(
                        stored.get("integrity_report"),
                        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
                        name="collector_run.integrity_report",
                    )
                    report_file = dict(report_value)
                    for key, expected_value in (
                        ("checker_version", CHECKER_VERSION),
                        ("error_count", 0),
                        ("valid", True),
                        ("warning_count", 0),
                    ):
                        if report_file.pop(key, None) != expected_value:
                            raise R25PostRunIntegrityError(
                                "INVALID_COLLECTOR_INTEGRITY_REPORT",
                                "report summary differs",
                            )
                    report_name = f"pilot-cell-{locator.sequence_index:03d}.integrity.v1.json"
                    report_path = report_root / report_name
                    report_raw, _ = _reopen_file_record_at(
                        cast(JsonValue, report_file),
                        directory_fd=report_root_fd,
                        expected_path=report_path,
                        maximum_bytes=_MAX_INTEGRITY_REPORT_BYTES,
                        code="INVALID_COLLECTOR_INTEGRITY_REPORT",
                        require_canonical=False,
                    )
                    report_document = _decode_canonical_json_line(
                        report_raw, code="INVALID_COLLECTOR_INTEGRITY_REPORT"
                    )
                    _report_projection(report_document)
                    expected_record = {
                        "arm": locator.arm,
                        "cleanup_evidence_sha256": locator.cleanup_evidence_sha256,
                        "collector_manifest_final": {
                            "byte_count": locator.manifest_final_byte_count,
                            "path_identity": cast(JsonValue, manifest_identity),
                            "sha256": locator.manifest_final_sha256,
                        },
                        "collector_run_id": locator.collector_run_id,
                        "collector_run_root_identity": cast(
                            JsonValue,
                            _path_identity(run_root, run_metadata, kind="DIRECTORY"),
                        ),
                        "collector_task_run_id": locator.task_run_id,
                        "host": locator.host,
                        "integrity_report": report_value,
                        "official_result_sha256": locator.official_result_evidence_sha256,
                        "reset_evidence_sha256": locator.reset_evidence_sha256,
                        "sequence_index": locator.sequence_index,
                        "task_id": locator.task_id,
                        "unit_id": locator.unit_id,
                        "unit_journal_sha256": locator.unit_journal_sha256,
                    }
                    if stored != expected_record:
                        raise R25PostRunIntegrityError(
                            "POST_RUN_INTEGRITY_BINDING_MISMATCH",
                            "Collector acceptance record differs",
                        )
                    if rerun_official_checker:
                        checked = _report_projection(
                            _bounded_official_integrity_check(
                                run_root_fd,
                                collector_run_id=locator.collector_run_id,
                                deadline_ns=reopen_deadline_ns,
                                public_path=None,
                            )
                        )
                        for key in (
                            "checker_version",
                            "counts",
                            "errors",
                            "valid",
                            "warnings",
                        ):
                            if checked.get(key) != report_document.get(key):
                                raise R25PostRunIntegrityError(
                                    "COLLECTOR_INTEGRITY_RECHECK_MISMATCH",
                                    "official Collector recheck differs from durable report",
                                )
            if artifact.get(
                "ordered_pilot_collector_integrity_root_sha256"
            ) != _ordered_collector_root(
                cast(list[dict[str, JsonValue]], stored_records)
            ) or artifact.get(
                "ordered_smoke_collector_integrity_root_sha256"
            ) != _ordered_collector_root(cast(list[dict[str, JsonValue]], stored_smoke_records)):
                raise R25PostRunIntegrityError(
                    "POST_RUN_INTEGRITY_BINDING_MISMATCH", "ordered Collector root differs"
                )
    return artifact, _sha_bytes(raw)


def reopen_validate_post_run_integrity_capability_v1(
    artifact_path: Path,
    *,
    repository_root: Path,
    authority: PostRunIntegrityAuthorityV1,
) -> ValidatedPostRunIntegrityArtifactV1:
    """Strictly recheck an aggregate and issue an unforgeable in-process capability."""

    artifact, artifact_sha256 = reopen_validate_post_run_integrity_artifact_v1(
        artifact_path,
        repository_root=repository_root,
        authority=authority,
        rerun_official_checker=True,
    )
    return ValidatedPostRunIntegrityArtifactV1(
        artifact_path=str(artifact_path),
        artifact_sha256=artifact_sha256,
        ordered_collector_integrity_root_sha256=cast(
            str, artifact["ordered_collector_integrity_root_sha256"]
        ),
        authority_manifest_sha256=authority.authority_manifest_sha256,
        pilot_manifest_sha256=authority.pilot_manifest_sha256,
        resolved_pilot_inputs_sha256=authority.resolved_pilot_inputs_sha256,
        preflight_report_sha256=authority.preflight_report_sha256,
        runtime_config_sha256=authority.runtime_config_sha256,
        pricing_sha256=authority.pricing_sha256,
        sentinel_config_sha256=authority.sentinel_config_sha256,
        factory_binding_sha256=authority.factory_binding_sha256,
        backend_endpoint=authority.backend_endpoint,
        source_commit=authority.source_commit,
        collector_run_count=cast(int, artifact["collector_run_count"]),
        pilot_collector_run_count=cast(int, artifact["pilot_collector_run_count"]),
        smoke_collector_run_count=cast(int, artifact["smoke_collector_run_count"]),
        ordered_pilot_collector_integrity_root_sha256=cast(
            str, artifact["ordered_pilot_collector_integrity_root_sha256"]
        ),
        ordered_smoke_collector_integrity_root_sha256=cast(
            str, artifact["ordered_smoke_collector_integrity_root_sha256"]
        ),
        production_audit_root_identity_sha256=cast(
            str | None, artifact["production_audit_root_identity_sha256"]
        ),
        _artifact_raw=canonical_json_bytes(cast(JsonValue, artifact)),
        _seal=_VALIDATION_SEAL,
    )


def validated_post_run_integrity_projection_v1(
    capability: ValidatedPostRunIntegrityArtifactV1,
) -> dict[str, JsonValue]:
    """Return a fresh detached aggregate only from a module-issued capability."""

    if (
        type(capability) is not ValidatedPostRunIntegrityArtifactV1
        or capability._seal is not _VALIDATION_SEAL
    ):
        raise R25PostRunIntegrityError(
            "UNTRUSTED_POST_RUN_INTEGRITY_CAPABILITY", "capability type/seal differs"
        )
    capability.__post_init__()
    return _decode_canonical_json(
        capability._artifact_raw,
        code="INVALID_POST_RUN_INTEGRITY_ARTIFACT",
    )


__all__ = [
    "CHECKER_VERSION",
    "CollectorRunLocatorV1",
    "SmokeCollectorRunLocatorV1",
    "POST_RUN_INTEGRITY_COMPLETION_SCHEMA_VERSION",
    "POST_RUN_INTEGRITY_SCHEMA_VERSION",
    "POST_RUN_INTEGRITY_WARNINGS_POLICY",
    "PostRunIntegrityAuthorityV1",
    "R25PostRunIntegrityError",
    "ValidatedPostRunIntegrityArtifactV1",
    "collector_run_locators_from_pilot_evidence_v1",
    "smoke_collector_run_locators_from_stage_evidence_v2",
    "derived_post_run_integrity_root_v1",
    "reopen_validate_post_run_integrity_artifact_v1",
    "reopen_validate_post_run_integrity_capability_v1",
    "run_post_run_integrity_gate_v1",
    "validate_durable_unit_journal_projection_v1",
    "validate_pilot_stage_durable_evidence_projection_v2",
    "validate_resource_preflight_stage_evidence_projection_v1",
    "validate_smoke_stage_durable_evidence_projection_v2",
    "validated_post_run_integrity_projection_v1",
]
