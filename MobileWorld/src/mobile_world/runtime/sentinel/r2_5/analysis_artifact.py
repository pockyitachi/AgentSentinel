"""Strict repo-external publication for one complete R2.5 pilot analysis."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
)
from mobile_world.runtime.sentinel.r2_5.analysis import (
    PilotAnalysisEvidenceCompletenessV1,
    PilotAnalysisProductionBindingsV1,
    PilotAnalysisV1,
    analyze_pilot_stage_v1,
    pilot_analysis_projection,
    pilot_analysis_sha256,
)
from mobile_world.runtime.sentinel.r2_5.integrity_gate import (
    PostRunIntegrityAuthorityV1,
    R25PostRunIntegrityError,
    ValidatedPostRunIntegrityArtifactV1,
    reopen_validate_post_run_integrity_capability_v1,
    validated_post_run_integrity_projection_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import FrozenPilotManifestV1

_MAX_STAGE_ARTIFACT_BYTES = 65 * 1024 * 1024
_MAX_AUDIT_DETAIL_BYTES = 256 * 1024 * 1024
_STAGE_WRAPPER_FIELDS = frozenset({"evidence", "receipt", "schema_version"})
_STAGE_RECEIPT_FIELDS = frozenset(
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


class R25AnalysisArtifactError(RuntimeError):
    """Stable fail-closed error for analysis input/output artifacts."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


@dataclass(frozen=True, slots=True)
class _DirectoryLinkV1:
    parent_descriptor: int
    descriptor: int
    name: str
    identity: tuple[int, int, int, int, int]


class _BoundDirectoryV1:
    """An absolute directory chain held open across every child operation."""

    def __init__(self, path: Path, *, code: str) -> None:
        if (
            not path.is_absolute()
            or not path.parts
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise R25AnalysisArtifactError(code, "directory path is not exact absolute text")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise R25AnalysisArtifactError(code, "O_NOFOLLOW is unavailable")
        flags |= no_follow
        descriptors: list[int] = []
        links: list[_DirectoryLinkV1] = []
        try:
            root_descriptor = os.open("/", flags)
            descriptors.append(root_descriptor)
            parent_descriptor = root_descriptor
            for name in path.parts[1:]:
                descriptor = os.open(name, flags, dir_fd=parent_descriptor)
                metadata = os.fstat(descriptor)
                named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                identity = (
                    metadata.st_dev,
                    metadata.st_ino,
                    stat.S_IFMT(metadata.st_mode),
                    metadata.st_uid,
                    metadata.st_gid,
                )
                if identity != (
                    named.st_dev,
                    named.st_ino,
                    stat.S_IFMT(named.st_mode),
                    named.st_uid,
                    named.st_gid,
                ) or not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("directory identity changed while opening")
                links.append(
                    _DirectoryLinkV1(
                        parent_descriptor=parent_descriptor,
                        descriptor=descriptor,
                        name=name,
                        identity=identity,
                    )
                )
                descriptors.append(descriptor)
                parent_descriptor = descriptor
        except OSError as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise R25AnalysisArtifactError(code, "directory chain is unavailable") from exc
        self._path = path
        self._code = code
        self._descriptors = descriptors
        self._links = links

    @property
    def descriptor(self) -> int:
        return self._descriptors[-1]

    @property
    def identities(self) -> frozenset[tuple[int, int]]:
        return frozenset((item.identity[0], item.identity[1]) for item in self._links)

    @property
    def final_identity(self) -> tuple[int, int]:
        metadata = os.fstat(self.descriptor)
        return metadata.st_dev, metadata.st_ino

    def require_owner_only(self) -> None:
        metadata = os.fstat(self.descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
        ):
            raise R25AnalysisArtifactError(self._code, "directory is not an owner-only real path")

    def verify(self) -> None:
        try:
            for link in self._links:
                metadata = os.fstat(link.descriptor)
                named = os.stat(
                    link.name,
                    dir_fd=link.parent_descriptor,
                    follow_symlinks=False,
                )
                observed = (
                    metadata.st_dev,
                    metadata.st_ino,
                    stat.S_IFMT(metadata.st_mode),
                    metadata.st_uid,
                    metadata.st_gid,
                )
                named_identity = (
                    named.st_dev,
                    named.st_ino,
                    stat.S_IFMT(named.st_mode),
                    named.st_uid,
                    named.st_gid,
                )
                if observed != link.identity or named_identity != link.identity:
                    raise OSError("directory identity changed")
        except OSError as exc:
            raise R25AnalysisArtifactError(
                self._code, "directory chain changed during the operation"
            ) from exc

    def close(self) -> None:
        for descriptor in reversed(self._descriptors):
            os.close(descriptor)


@contextmanager
def _bound_owner_directory(
    path: Path, *, code: str, require_owner_only: bool = True
) -> Iterator[_BoundDirectoryV1]:
    bound = _BoundDirectoryV1(path, code=code)
    try:
        if require_owner_only:
            bound.require_owner_only()
        yield bound
        bound.verify()
    finally:
        bound.close()


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _read_owner_file_at(
    directory: _BoundDirectoryV1,
    filename: str,
    *,
    maximum_bytes: int,
    code: str,
) -> JsonValue:
    if (
        type(filename) is not str
        or not filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise R25AnalysisArtifactError(code, "file name is invalid")
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise R25AnalysisArtifactError(code, "O_NOFOLLOW is unavailable")
        before = os.stat(filename, dir_fd=directory.descriptor, follow_symlinks=False)
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
            dir_fd=directory.descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum_bytes
            or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise R25AnalysisArtifactError(code, "file metadata differs from the contract")
        remaining = metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                raise R25AnalysisArtifactError(code, "file is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise R25AnalysisArtifactError(code, "file grew during the read")
        after = os.fstat(descriptor)
        named_after = os.stat(filename, dir_fd=directory.descriptor, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(after, name) != getattr(metadata, name) for name in stable_fields) or any(
            getattr(named_after, name) != getattr(metadata, name) for name in stable_fields
        ):
            raise R25AnalysisArtifactError(code, "file identity changed during the read")
        raw = b"".join(chunks)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except R25AnalysisArtifactError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise R25AnalysisArtifactError(code, "file is not canonical JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if canonical_json_bytes(cast(JsonValue, value)) != raw:
        raise R25AnalysisArtifactError(code, "file is not exact canonical JSON")
    return cast(JsonValue, value)


def _stage_evidence(
    value: JsonValue,
    *,
    manifest_sha256: str,
    run_id: str,
    expected_cells: int,
) -> dict[str, JsonValue]:
    if type(value) is not dict or set(value) != _STAGE_WRAPPER_FIELDS:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "wrapper differs")
    if value.get("schema_version") != LIVE_EXECUTOR_BINDING_SCHEMA_VERSION:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "schema differs")
    evidence = value.get("evidence")
    receipt = value.get("receipt")
    if type(evidence) is not dict or type(receipt) is not dict:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "payload differs")
    if set(receipt) != _STAGE_RECEIPT_FIELDS:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "receipt differs")
    evidence_hash = hashlib.sha256(canonical_json_bytes(cast(JsonValue, evidence))).hexdigest()
    completed = receipt.get("completed_units")
    expected_units = [f"pilot-cell-{index:03d}" for index in range(expected_cells)]
    census = evidence.get("census")
    if type(census) is not dict:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "census is absent")
    if not all(
        _is_nonnegative_int(receipt.get(name))
        for name in (
            "actor_actions",
            "actor_calls",
            "cost_usd_micros",
            "openai_calls",
            "wall_time_ms",
        )
    ):
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "receipt census differs")
    exact_census = (
        receipt.get("actor_actions") == census.get("actor_actions")
        and receipt.get("actor_calls") == census.get("actor_calls")
        and receipt.get("cost_usd_micros") == census.get("cost_usd_micros")
        and receipt.get("openai_calls") == census.get("openai_calls")
    )
    if (
        receipt.get("stage") != "R25_PILOT"
        or receipt.get("passed") is not True
        or receipt.get("provider_final_request_proven") is not True
        or receipt.get("manifest_sha256") != manifest_sha256
        or receipt.get("evidence_sha256") != evidence_hash
        or completed != expected_units
        or evidence.get("manifest_sha256") != manifest_sha256
        or evidence.get("run_id") != run_id
        or not exact_census
    ):
        raise R25AnalysisArtifactError(
            "PILOT_STAGE_BINDING_MISMATCH", "stage receipt and evidence differ"
        )
    return evidence


def _audit_references(evidence: dict[str, JsonValue]) -> tuple[tuple[str, str], ...]:
    cells = evidence.get("cells")
    if type(cells) is not list:
        raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "cells differ")
    references: list[tuple[str, str]] = []
    for cell in cells:
        if type(cell) is not dict or type(cell.get("decisions")) is not list:
            raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "decisions differ")
        for decision in cast(list[object], cell["decisions"]):
            if type(decision) is not dict:
                raise R25AnalysisArtifactError("INVALID_PILOT_STAGE_ARTIFACT", "decision differs")
            logical_call_id = decision.get("logical_call_id")
            detail_hash = decision.get("runtime_audit_detail_sha256")
            if (
                type(logical_call_id) is not str
                or not logical_call_id
                or "/" in logical_call_id
                or "\\" in logical_call_id
                or logical_call_id in {".", ".."}
                or type(detail_hash) is not str
                or len(detail_hash) != 64
                or any(character not in "0123456789abcdef" for character in detail_hash)
            ):
                raise R25AnalysisArtifactError(
                    "INVALID_PILOT_STAGE_ARTIFACT", "audit reference differs"
                )
            references.append((logical_call_id, detail_hash))
    if len({logical_call_id for logical_call_id, _ in references}) != len(references):
        raise R25AnalysisArtifactError(
            "INVALID_PILOT_STAGE_ARTIFACT", "logical call IDs are not unique"
        )
    return tuple(references)


def analyze_pilot_artifacts_v1(
    manifest: FrozenPilotManifestV1,
    *,
    run_manifest_sha256: str,
    run_id: str,
    pilot_stage_artifact: Path,
    production_audit_root: Path,
    post_run_integrity_artifact: Path | None = None,
    confirmed_post_run_integrity_sha256: str | None = None,
    post_run_integrity_authority: PostRunIntegrityAuthorityV1 | None = None,
) -> PilotAnalysisV1:
    """Load one complete stage and every referenced audit detail, then analyze.

    Omitting all post-run integrity inputs retains the CPU/legacy analysis path,
    whose result is deliberately unpublishable. Production publication requires
    all three inputs and independently reopens the complete sequence, all 80
    Collector reports, and the durable post-run acceptance artifact.
    """

    integrity_inputs = (
        post_run_integrity_artifact,
        confirmed_post_run_integrity_sha256,
        post_run_integrity_authority,
    )
    if any(item is not None for item in integrity_inputs) and any(
        item is None for item in integrity_inputs
    ):
        raise R25AnalysisArtifactError(
            "POST_RUN_INTEGRITY_BINDING_REQUIRED",
            "production analysis requires the complete post-run integrity binding",
        )
    production_bindings: PilotAnalysisProductionBindingsV1 | None = None
    expected_pilot_stage_record: dict[str, JsonValue] | None = None
    if post_run_integrity_authority is not None:
        if (
            type(post_run_integrity_authority) is not PostRunIntegrityAuthorityV1
            or not isinstance(post_run_integrity_artifact, Path)
            or type(confirmed_post_run_integrity_sha256) is not str
            or len(confirmed_post_run_integrity_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in confirmed_post_run_integrity_sha256
            )
            or post_run_integrity_authority.run_id != run_id
            or post_run_integrity_authority.authority_manifest_sha256 != run_manifest_sha256
            or post_run_integrity_authority.pilot_manifest != manifest
            or post_run_integrity_authority.production_audit_root != str(production_audit_root)
        ):
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH",
                "post-run integrity authority differs from the analysis inputs",
            )
        try:
            capability = reopen_validate_post_run_integrity_capability_v1(
                post_run_integrity_artifact,
                repository_root=Path(__file__).resolve().parents[6],
                authority=post_run_integrity_authority,
            )
            accepted = validated_post_run_integrity_projection_v1(capability)
        except R25PostRunIntegrityError as exc:
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_REOPEN_FAILED",
                "post-run integrity evidence failed strict reopening",
            ) from exc
        if (
            type(capability) is not ValidatedPostRunIntegrityArtifactV1
            or capability.artifact_sha256 != confirmed_post_run_integrity_sha256
            or capability.artifact_path != str(post_run_integrity_artifact)
            or capability.authority_manifest_sha256 != run_manifest_sha256
            or capability.pilot_manifest_sha256
            != post_run_integrity_authority.pilot_manifest_sha256
            or capability.resolved_pilot_inputs_sha256
            != post_run_integrity_authority.resolved_pilot_inputs_sha256
            or capability.preflight_report_sha256
            != post_run_integrity_authority.preflight_report_sha256
            or capability.factory_binding_sha256
            != post_run_integrity_authority.factory_binding_sha256
            or capability.backend_endpoint != post_run_integrity_authority.backend_endpoint
            or capability.pilot_collector_run_count != len(manifest.cells)
            or capability.smoke_collector_run_count != 6
            or capability.collector_run_count != len(manifest.cells) + 6
        ):
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_CONFIRMATION_MISMATCH",
                "caller confirmation or capability roots differ from the strict reopen",
            )
        stage_records = accepted.get("stage_files")
        if type(stage_records) is not list:
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH", "stage file binding is absent"
            )
        matching_stage_records = [
            item
            for item in stage_records
            if type(item) is dict and item.get("stage") == "R25_PILOT"
        ]
        if len(matching_stage_records) != 1:
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH", "pilot stage census differs"
            )
        expected_pilot_stage_record = matching_stage_records[0]
        ordered_root = accepted.get("ordered_collector_integrity_root_sha256")
        if type(ordered_root) is not str:
            raise R25AnalysisArtifactError(
                "POST_RUN_INTEGRITY_BINDING_MISMATCH", "ordered Collector root is absent"
            )
        production_bindings = PilotAnalysisProductionBindingsV1(
            authority_manifest_sha256=run_manifest_sha256,
            run_id=run_id,
            run_manifest=post_run_integrity_authority.run_manifest,
            resolved_pilot_inputs_sha256=(
                post_run_integrity_authority.resolved_pilot_inputs_sha256
            ),
            backend_endpoint=post_run_integrity_authority.backend_endpoint,
            preflight_report_sha256=(post_run_integrity_authority.preflight_report_sha256),
            factory_binding_sha256=post_run_integrity_authority.factory_binding_sha256,
            post_run_integrity_artifact_sha256=capability.artifact_sha256,
            ordered_collector_integrity_root_sha256=ordered_root,
            _validated_integrity=capability,
        )

    if not pilot_stage_artifact.is_absolute():
        raise R25AnalysisArtifactError(
            "INVALID_PILOT_STAGE_ARTIFACT", "stage artifact path is not absolute"
        )
    with (
        _bound_owner_directory(
            production_audit_root, code="INVALID_PRODUCTION_AUDIT_ROOT"
        ) as audit_root,
        _bound_owner_directory(
            pilot_stage_artifact.parent, code="INVALID_PILOT_STAGE_ARTIFACT"
        ) as stage_root,
    ):
        stage_value = _read_owner_file_at(
            stage_root,
            pilot_stage_artifact.name,
            maximum_bytes=_MAX_STAGE_ARTIFACT_BYTES,
            code="INVALID_PILOT_STAGE_ARTIFACT",
        )
        if expected_pilot_stage_record is not None:
            path_identity = expected_pilot_stage_record.get("path_identity")
            if (
                type(path_identity) is not dict
                or path_identity.get("canonical_path") != str(pilot_stage_artifact)
                or expected_pilot_stage_record.get("byte_count")
                != len(canonical_json_bytes(stage_value))
                or expected_pilot_stage_record.get("sha256")
                != hashlib.sha256(canonical_json_bytes(stage_value)).hexdigest()
            ):
                raise R25AnalysisArtifactError(
                    "POST_RUN_INTEGRITY_BINDING_MISMATCH",
                    "pilot stage path or bytes differ from post-run acceptance",
                )
        evidence = _stage_evidence(
            stage_value,
            manifest_sha256=run_manifest_sha256,
            run_id=run_id,
            expected_cells=len(manifest.cells),
        )
        details: dict[str, JsonValue] = {}
        for logical_call_id, expected_hash in _audit_references(evidence):
            filename = f"{logical_call_id}.production-runtime-audit.v1.json"
            detail = _read_owner_file_at(
                audit_root,
                filename,
                maximum_bytes=_MAX_AUDIT_DETAIL_BYTES,
                code="AUDIT_DETAIL_UNAVAILABLE",
            )
            if hashlib.sha256(canonical_json_bytes(detail)).hexdigest() != expected_hash:
                raise R25AnalysisArtifactError(
                    "AUDIT_DETAIL_HASH_MISMATCH", "audit detail differs from stage evidence"
                )
            details[expected_hash] = detail
        audit_root.verify()
        stage_root.verify()
    return analyze_pilot_stage_v1(
        manifest,
        evidence,
        audit_detail_projections=details,
        production_bindings=production_bindings,
    )


def write_pilot_analysis_artifact_v1(
    analysis: PilotAnalysisV1,
    output: Path,
    *,
    repository_root: Path,
) -> str:
    """Write a fresh canonical 0600 artifact into an owner-only external directory."""

    if (
        type(analysis) is not PilotAnalysisV1
        or analysis.evidence_completeness
        is not PilotAnalysisEvidenceCompletenessV1.PRODUCTION_V2_INTEGRITY_BOUND
    ):
        raise R25AnalysisArtifactError(
            "LEGACY_ANALYSIS_PUBLICATION_FORBIDDEN",
            "only production-v2 analysis bound to the post-run integrity gate is publishable",
        )
    if (
        not output.is_absolute()
        or not output.name
        or "/" in output.name
        or "\\" in output.name
        or output.name in {".", ".."}
    ):
        raise R25AnalysisArtifactError("INVALID_ANALYSIS_OUTPUT", "output is not fresh")
    payload = canonical_json_bytes(cast(JsonValue, pilot_analysis_projection(analysis)))
    descriptor = -1
    with (
        _bound_owner_directory(output.parent, code="INVALID_ANALYSIS_OUTPUT") as parent,
        _bound_owner_directory(
            repository_root,
            code="INVALID_ANALYSIS_OUTPUT",
            require_owner_only=False,
        ) as repository,
    ):
        if (
            repository.final_identity in parent.identities
            or parent.final_identity in repository.identities
            or repository.final_identity == parent.final_identity
        ):
            raise R25AnalysisArtifactError(
                "INVALID_ANALYSIS_OUTPUT", "analysis output must stay outside the repository"
            )
        try:
            descriptor = os.open(
                output.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent.descriptor,
            )
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short analysis artifact write")
                remaining = remaining[written:]
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            named = os.stat(output.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_nlink != 1
                or metadata.st_size != len(payload)
                or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise OSError("analysis artifact metadata changed")
            os.fsync(parent.descriptor)
            parent.verify()
            repository.verify()
        except OSError as exc:
            raise R25AnalysisArtifactError(
                "ANALYSIS_ARTIFACT_WRITE_FAILED",
                "analysis artifact publication failed; any created bytes were retained",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return pilot_analysis_sha256(analysis)


__all__ = [
    "R25AnalysisArtifactError",
    "analyze_pilot_artifacts_v1",
    "write_pilot_analysis_artifact_v1",
]
