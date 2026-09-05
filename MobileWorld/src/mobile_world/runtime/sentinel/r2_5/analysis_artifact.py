"""Strict repo-external publication for one complete R2.5 pilot analysis."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
)
from mobile_world.runtime.sentinel.r2_5.analysis import (
    PilotAnalysisV1,
    analyze_pilot_stage_v1,
    pilot_analysis_projection,
    pilot_analysis_sha256,
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


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _require_owner_directory(path: Path, *, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise R25AnalysisArtifactError(code, "directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or resolved != path.absolute()
    ):
        raise R25AnalysisArtifactError(code, "directory is not an owner-only real path")
    return resolved


def _read_owner_file(path: Path, *, maximum_bytes: int, code: str) -> JsonValue:
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise R25AnalysisArtifactError(code, "O_NOFOLLOW is unavailable")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum_bytes
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
) -> PilotAnalysisV1:
    """Load one complete stage and every referenced audit detail, then analyze."""

    audit_root = _require_owner_directory(
        production_audit_root, code="INVALID_PRODUCTION_AUDIT_ROOT"
    )
    stage_value = _read_owner_file(
        pilot_stage_artifact,
        maximum_bytes=_MAX_STAGE_ARTIFACT_BYTES,
        code="INVALID_PILOT_STAGE_ARTIFACT",
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
        path = audit_root / filename
        detail = _read_owner_file(
            path,
            maximum_bytes=_MAX_AUDIT_DETAIL_BYTES,
            code="AUDIT_DETAIL_UNAVAILABLE",
        )
        if hashlib.sha256(canonical_json_bytes(detail)).hexdigest() != expected_hash:
            raise R25AnalysisArtifactError(
                "AUDIT_DETAIL_HASH_MISMATCH", "audit detail differs from stage evidence"
            )
        details[expected_hash] = detail
    return analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)


def write_pilot_analysis_artifact_v1(
    analysis: PilotAnalysisV1,
    output: Path,
    *,
    repository_root: Path,
) -> str:
    """Write a fresh canonical 0600 artifact into an owner-only external directory."""

    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise R25AnalysisArtifactError("INVALID_ANALYSIS_OUTPUT", "output is not fresh")
    parent = _require_owner_directory(output.parent, code="INVALID_ANALYSIS_OUTPUT")
    try:
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise R25AnalysisArtifactError(
            "INVALID_ANALYSIS_OUTPUT", "repository is unavailable"
        ) from exc
    resolved = parent / output.name
    if _is_within(resolved, repository) or _is_within(repository, resolved):
        raise R25AnalysisArtifactError(
            "INVALID_ANALYSIS_OUTPUT", "analysis output must stay outside the repository"
        )
    payload = canonical_json_bytes(cast(JsonValue, pilot_analysis_projection(analysis)))
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            resolved,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short analysis artifact write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
        ):
            raise OSError("analysis artifact metadata changed")
        directory_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if created:
            try:
                os.unlink(resolved)
            except OSError:
                pass
        raise R25AnalysisArtifactError(
            "ANALYSIS_ARTIFACT_WRITE_FAILED", "analysis artifact was not published"
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
