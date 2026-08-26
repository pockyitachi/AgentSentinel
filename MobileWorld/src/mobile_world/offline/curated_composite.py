"""Build and verify a zero-copy curated task-set manifest.

The output is deliberately a derived selection manifest, not a synthetic audit
run.  Selected event streams and their transitive blobs remain in their
immutable source runs and are addressed through an explicit source locator.
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
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_world.runtime.audit.schemas import SchemaValidationError, validate_event_envelope

COMPOSITE_SCHEMA_VERSION = "mobileworld.audit.curated-task-set/v1"
VALIDATION_SCHEMA_VERSION = "mobileworld.audit.curated-validation/v1"
CHECKER_VERSION = "mobileworld.audit.curated-builder/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BLOB_FIELDS = frozenset({"algorithm", "digest", "byte_length", "media_type", "relative_path"})
_UNIQUE_CANDIDATE_RESOLUTION = "exactly_one_eligible_stream_per_canonical_task"
_PINNED_CANDIDATE_RESOLUTION = (
    "exact_task_source_pin_else_exactly_one_eligible_stream_per_canonical_task"
)
_TASK_SOURCE_PIN_KEYS = frozenset(
    {
        "canonical_suite_index",
        "task_name",
        "source_id",
        "source_task_run_id",
    }
)
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


class CompositeBuildError(RuntimeError):
    """A source or requested composite violates a deterministic invariant."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TaskSourcePin:
    """Select one exact eligible raw task stream for one canonical task."""

    canonical_suite_index: int
    task_name: str
    source_id: str
    source_task_run_id: str

    def as_manifest_record(self) -> dict[str, Any]:
        return {
            "canonical_suite_index": self.canonical_suite_index,
            "task_name": self.task_name,
            "source_id": self.source_id,
            "source_task_run_id": self.source_task_run_id,
        }


@dataclass(frozen=True, slots=True)
class _FileSummary:
    sha256: str
    byte_count: int

    def as_dict(self, relative_path: str) -> dict[str, Any]:
        return {
            "relative_path": relative_path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class _TaskCandidate:
    source_id: str
    source_root: Path
    source_run_id: str
    raw_schema_version: str
    task_run_id: str
    stream_relative_path: str
    stream_path: Path
    stream_summary: _FileSummary
    stream_events: tuple[dict[str, Any], ...]
    summary_runtime_status: str | None
    summary_capture_complete: bool | None
    summary_missing_artifacts: tuple[str, ...]
    summary_collector_error_event_ids: tuple[str, ...]
    task_name: str
    source_task_index: int
    whole_task_attempt_index: int
    task_goal: str
    task_started_event_id: str
    task_ended_event_id: str
    runtime_status: str | None
    capture_complete: bool | None
    missing_artifacts: tuple[str, ...]
    collector_error_event_ids: tuple[str, ...]
    environment_score: int | float | None
    environment_reason: str | None

    @property
    def eligible(self) -> bool:
        return (
            self.summary_runtime_status == "completed"
            and self.summary_capture_complete is True
            and not self.summary_missing_artifacts
            and not self.summary_collector_error_event_ids
            and self.runtime_status == "completed"
            and self.capture_complete is True
            and not self.missing_artifacts
            and not self.collector_error_event_ids
        )


@dataclass(frozen=True, slots=True)
class _SourceScan:
    source_id: str
    root: Path
    relative_run_path: str
    run_id: str
    raw_schema_version: str
    start: dict[str, Any]
    final: dict[str, Any]
    manifest_start: _FileSummary
    manifest_final: _FileSummary
    run_events: _FileSummary
    candidates: tuple[_TaskCandidate, ...]


@dataclass(frozen=True, slots=True)
class _BlobClosureStats:
    reference_occurrences: int
    unique_blob_count: int
    unique_blob_byte_count: int


@dataclass(frozen=True, slots=True)
class _PreparedComposite:
    manifest: dict[str, Any]
    checks: dict[str, bool]
    warnings: tuple[dict[str, Any], ...]


def _normalize_task_source_pins(
    values: Sequence[TaskSourcePin],
) -> tuple[TaskSourcePin, ...]:
    _require(
        isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)),
        "task_source_pins_shape_invalid",
        "task source pins must be a sequence",
    )
    normalized: list[TaskSourcePin] = []
    seen_indices: set[int] = set()
    seen_names: set[str] = set()
    seen_streams: set[tuple[str, str]] = set()
    for offset, value in enumerate(values):
        _require(
            isinstance(value, TaskSourcePin),
            "task_source_pin_shape_invalid",
            "each task source pin must be a TaskSourcePin",
            pin_offset=offset,
        )
        index = _positive_int(
            value.canonical_suite_index,
            f"task_source_pins[{offset}].canonical_suite_index",
        )
        task_name = _string(value.task_name, f"task_source_pins[{offset}].task_name")
        source_id = _string(value.source_id, f"task_source_pins[{offset}].source_id")
        _require(
            bool(_SOURCE_ID_RE.fullmatch(source_id)),
            "task_source_pin_source_id_invalid",
            "a pinned source ID must be lowercase and filesystem-neutral",
            pin_offset=offset,
            source_id=source_id,
        )
        source_task_run_id = _string(
            value.source_task_run_id,
            f"task_source_pins[{offset}].source_task_run_id",
        )
        _require(
            index not in seen_indices,
            "task_source_pin_duplicate_canonical_index",
            "task source pins must not repeat a canonical suite index",
            canonical_suite_index=index,
        )
        _require(
            task_name not in seen_names,
            "task_source_pin_duplicate_task_name",
            "task source pins must not repeat a canonical task name",
            task_name=task_name,
        )
        stream_identity = (source_id, source_task_run_id)
        _require(
            stream_identity not in seen_streams,
            "task_source_pin_duplicate_stream",
            "one raw task stream must not be pinned to multiple canonical tasks",
            source_id=source_id,
            source_task_run_id=source_task_run_id,
        )
        seen_indices.add(index)
        seen_names.add(task_name)
        seen_streams.add(stream_identity)
        normalized.append(
            TaskSourcePin(
                canonical_suite_index=index,
                task_name=task_name,
                source_id=source_id,
                source_task_run_id=source_task_run_id,
            )
        )
    return tuple(
        sorted(
            normalized,
            key=lambda pin: (
                pin.canonical_suite_index,
                pin.task_name,
                pin.source_id,
                pin.source_task_run_id,
            ),
        )
    )


def _task_source_pins_from_manifest(
    selection_policy: Mapping[str, Any],
) -> tuple[TaskSourcePin, ...]:
    resolution = _string(
        selection_policy.get("candidate_resolution"),
        "selection_policy.candidate_resolution",
    )
    has_pins = "task_source_pins" in selection_policy
    if resolution == _UNIQUE_CANDIDATE_RESOLUTION:
        _require(
            not has_pins,
            "task_source_pins_unexpected",
            "the default unique-candidate policy must not declare task source pins",
        )
        return ()
    _require(
        resolution == _PINNED_CANDIDATE_RESOLUTION,
        "candidate_resolution_unsupported",
        "the manifest declares an unsupported candidate-resolution policy",
        candidate_resolution=resolution,
    )
    raw_pins = selection_policy.get("task_source_pins")
    _require(
        isinstance(raw_pins, list) and bool(raw_pins),
        "task_source_pins_missing",
        "the pinned candidate-resolution policy requires a non-empty pin list",
    )
    pins: list[TaskSourcePin] = []
    for offset, value in enumerate(raw_pins):
        pin = _mapping(value, f"selection_policy.task_source_pins[{offset}]")
        _require(
            set(pin) == _TASK_SOURCE_PIN_KEYS,
            "task_source_pin_keys_invalid",
            "a manifest task source pin has unexpected or missing fields",
            pin_offset=offset,
            actual_keys=sorted(pin),
            expected_keys=sorted(_TASK_SOURCE_PIN_KEYS),
        )
        pins.append(
            TaskSourcePin(
                canonical_suite_index=_positive_int(
                    pin.get("canonical_suite_index"),
                    (f"selection_policy.task_source_pins[{offset}].canonical_suite_index"),
                ),
                task_name=_string(
                    pin.get("task_name"),
                    f"selection_policy.task_source_pins[{offset}].task_name",
                ),
                source_id=_string(
                    pin.get("source_id"),
                    f"selection_policy.task_source_pins[{offset}].source_id",
                ),
                source_task_run_id=_string(
                    pin.get("source_task_run_id"),
                    f"selection_policy.task_source_pins[{offset}].source_task_run_id",
                ),
            )
        )
    normalized = _normalize_task_source_pins(pins)
    _require(
        list(raw_pins) == [pin.as_manifest_record() for pin in normalized],
        "task_source_pin_order_invalid",
        "manifest task source pins must be in canonical deterministic order",
    )
    return normalized


def _validate_task_source_pins(
    pins: Sequence[TaskSourcePin],
    *,
    catalog: Sequence[Mapping[str, Any]],
    resolved_source_ids: frozenset[str],
) -> dict[str, TaskSourcePin]:
    catalog_by_index = {entry["canonical_suite_index"]: entry for entry in catalog}
    catalog_by_name = {entry["task_name"]: entry for entry in catalog}
    result: dict[str, TaskSourcePin] = {}
    for pin in pins:
        indexed = catalog_by_index.get(pin.canonical_suite_index)
        named = catalog_by_name.get(pin.task_name)
        _require(
            indexed is not None
            and named is not None
            and indexed["task_name"] == pin.task_name
            and named["canonical_suite_index"] == pin.canonical_suite_index,
            "task_source_pin_catalog_mismatch",
            "a task source pin must bind the exact canonical task name and index",
            **pin.as_manifest_record(),
            task_name_at_index=(indexed or {}).get("task_name"),
            canonical_index_for_name=(named or {}).get("canonical_suite_index"),
        )
        _require(
            pin.source_id in resolved_source_ids,
            "task_source_pin_source_missing",
            "a task source pin refers to a source ID that was not supplied",
            **pin.as_manifest_record(),
        )
        result[pin.task_name] = pin
    return result


def build_curated_composite(
    *,
    source_base: str | os.PathLike[str],
    sources: Mapping[str, str | os.PathLike[str]],
    catalog_source_id: str,
    expected_task_count: int,
    output_dir: str | os.PathLike[str],
    dataset_id: str | None = None,
    verify_blob_digests: bool = True,
    task_source_pins: Sequence[TaskSourcePin] = (),
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    """Validate sources and exclusively create a zero-copy derived dataset.

    No source file is opened for writing.  The output directory contains only
    ``manifest.json`` and ``validation_report.json``.
    """

    base = _resolve_existing_directory(Path(source_base), code="source_base_invalid")
    output = _resolve_output_path(Path(output_dir))
    resolved_sources = _resolve_sources(base, sources)
    _validate_output_location(output, resolved_sources.values())

    effective_dataset_id = dataset_id or output.name
    _validate_dataset_id(effective_dataset_id)
    normalized_pins = _normalize_task_source_pins(task_source_pins)

    prepared = _prepare_composite(
        source_base=base,
        resolved_sources=resolved_sources,
        catalog_source_id=catalog_source_id,
        expected_task_count=expected_task_count,
        dataset_id=effective_dataset_id,
        verify_blob_digests=verify_blob_digests,
        task_source_pins=normalized_pins,
    )
    manifest_bytes = _canonical_json_bytes(prepared.manifest, newline=True)
    report = _validation_report(
        manifest=prepared.manifest,
        manifest_bytes=manifest_bytes,
        source_base=base,
        checks=prepared.checks,
        warnings=prepared.warnings,
        verify_blob_digests=verify_blob_digests,
    )

    _require(
        output.parent.is_dir(),
        "output_parent_missing",
        "output directory parent must already exist",
        output_parent=str(output.parent),
    )
    if output.exists():
        raise CompositeBuildError(
            "output_exists",
            "output directory already exists; derived datasets are never overwritten",
            output_dir=str(output),
        )

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    staging.chmod(0o700)
    try:
        _write_exclusive(staging / "manifest.json", manifest_bytes)
        _write_exclusive(
            staging / "validation_report.json", _canonical_json_bytes(report, newline=True)
        )
        _fsync_directory(staging)
        _rename_directory_noreplace(staging, output)
        _fsync_directory(output.parent)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    manifest_path = output / "manifest.json"
    report_path = output / "validation_report.json"
    return manifest_path, report_path, prepared.manifest, report


def validate_curated_composite(
    *,
    manifest_path: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
    verify_blob_digests: bool = True,
) -> dict[str, Any]:
    """Independently re-resolve and validate an existing composite manifest."""

    requested_path = Path(manifest_path)
    base = _resolve_existing_directory(Path(source_base), code="source_base_invalid")
    try:
        path = _resolve_existing_regular_file(requested_path, code="manifest_missing")
        manifest_bytes = _read_regular_file(path, "manifest_missing")
        manifest = _loads_strict(manifest_bytes)
        _require(
            isinstance(manifest, dict),
            "manifest_shape_invalid",
            "composite manifest must be a JSON object",
        )
        _require(
            manifest_bytes == _canonical_json_bytes(manifest, newline=True),
            "manifest_not_canonical",
            "composite manifest bytes must be canonical JSON with one trailing newline",
        )
        _validate_manifest_header(manifest)
        source_specs = _source_specs_from_manifest(manifest)
        resolved_sources = _resolve_sources(base, source_specs)
        selection_policy = _mapping(manifest.get("selection_policy"), "selection_policy")
        catalog_source_id = _string(
            selection_policy.get("canonical_catalog_source_id"),
            "selection_policy.canonical_catalog_source_id",
        )
        catalog = _mapping(manifest.get("canonical_catalog"), "canonical_catalog")
        expected_count = _positive_int(catalog.get("task_count"), "canonical_catalog.task_count")
        task_source_pins = _task_source_pins_from_manifest(selection_policy)
        prepared = _prepare_composite(
            source_base=base,
            resolved_sources=resolved_sources,
            catalog_source_id=catalog_source_id,
            expected_task_count=expected_count,
            dataset_id=_validate_dataset_id(manifest.get("dataset_id")),
            verify_blob_digests=verify_blob_digests,
            task_source_pins=task_source_pins,
        )
        _require(
            manifest_bytes == _canonical_json_bytes(prepared.manifest, newline=True),
            "manifest_content_mismatch",
            "manifest content does not match the selected immutable source evidence",
        )
        return _validation_report(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            source_base=base,
            checks=prepared.checks,
            warnings=prepared.warnings,
            verify_blob_digests=verify_blob_digests,
        )
    except (
        CompositeBuildError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        if isinstance(error, CompositeBuildError):
            code = error.code
            context = error.context
            message = str(error)
        else:
            code = "manifest_validation_exception"
            context = {}
            message = f"{type(error).__name__}: {error}"
        return {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "checker_version": CHECKER_VERSION,
            "checked_at_utc": _utc_now(),
            "valid": False,
            "verification_mode": (
                "selected_streams_and_transitive_blob_sha256"
                if verify_blob_digests
                else "selected_streams_and_transitive_blob_size"
            ),
            "errors": [{"code": code, "message": message, **context}],
            "warnings": [],
            "checks": {},
            "counts": {},
        }


def write_validation_report(
    *,
    report: Mapping[str, Any],
    report_path: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
) -> Path:
    """Atomically write a report outside every referenced raw source tree."""

    base = _resolve_existing_directory(Path(source_base), code="source_base_invalid")
    manifest = _resolve_existing_regular_file(Path(manifest_path), code="manifest_missing")
    manifest_bytes = _read_regular_file(manifest, "manifest_missing")
    try:
        value = _loads_strict(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CompositeBuildError(
            "report_source_manifest_invalid",
            "cannot safely locate source raw roots from an invalid manifest",
        ) from error
    _require(
        isinstance(value, Mapping),
        "report_source_manifest_invalid",
        "cannot safely locate source raw roots from a non-object manifest",
    )
    resolved_sources = _resolve_sources(base, _source_specs_from_manifest(value))
    output = _resolve_output_path(Path(report_path))
    _validate_output_location(output, resolved_sources.values())
    _atomic_write_noreplace(output, _canonical_json_bytes(report, newline=True))
    return output


def _prepare_composite(
    *,
    source_base: Path,
    resolved_sources: Mapping[str, Path],
    catalog_source_id: str,
    expected_task_count: int,
    dataset_id: str,
    verify_blob_digests: bool,
    task_source_pins: tuple[TaskSourcePin, ...],
) -> _PreparedComposite:
    _require(
        catalog_source_id in resolved_sources,
        "catalog_source_missing",
        "catalog source ID must name one of the supplied sources",
        catalog_source_id=catalog_source_id,
    )
    _require(
        expected_task_count > 0,
        "expected_task_count_invalid",
        "expected task count must be positive",
        expected_task_count=expected_task_count,
    )

    scans = tuple(
        _scan_source(source_id, resolved_sources[source_id], source_base)
        for source_id in sorted(resolved_sources)
    )
    schemas = {scan.raw_schema_version for scan in scans}
    _require(
        len(schemas) == 1,
        "source_schema_mismatch",
        "all selected source runs must use the same raw schema version",
        raw_schema_versions=sorted(schemas),
    )

    catalog_scan = next(scan for scan in scans if scan.source_id == catalog_source_id)
    catalog = _build_catalog(catalog_scan, expected_task_count)
    catalog_by_name = {entry["task_name"]: entry for entry in catalog}
    pins_by_name = _validate_task_source_pins(
        task_source_pins,
        catalog=catalog,
        resolved_source_ids=frozenset(resolved_sources),
    )

    eligible_by_name: defaultdict[str, list[_TaskCandidate]] = defaultdict(list)
    for scan in scans:
        for candidate in scan.candidates:
            _require(
                candidate.task_name in catalog_by_name,
                "source_task_not_in_catalog",
                "a source run contains a task absent from the canonical catalog",
                source_id=scan.source_id,
                task_name=candidate.task_name,
            )
            if candidate.eligible:
                expected_goal = catalog_by_name[candidate.task_name]["task_goal_utf8_sha256"]
                actual_goal = _sha256(candidate.task_goal.encode("utf-8"))
                _require(
                    actual_goal == expected_goal,
                    "task_goal_mismatch",
                    "eligible replacement task goal differs from the canonical catalog goal",
                    source_id=scan.source_id,
                    task_name=candidate.task_name,
                )
                eligible_by_name[candidate.task_name].append(candidate)

    selected: list[tuple[dict[str, Any], _TaskCandidate]] = []
    for catalog_entry in catalog:
        name = catalog_entry["task_name"]
        candidates = eligible_by_name.get(name, [])
        pin = pins_by_name.get(name)
        if pin is None:
            _require(
                len(candidates) == 1,
                "eligible_task_cardinality",
                (
                    "every unpinned canonical task must have exactly one completed, "
                    "capture-complete source stream"
                ),
                task_name=name,
                canonical_suite_index=catalog_entry["canonical_suite_index"],
                eligible_task_run_ids=[candidate.task_run_id for candidate in candidates],
                eligible_source_ids=[candidate.source_id for candidate in candidates],
            )
            selected.append((catalog_entry, candidates[0]))
            continue

        matches = [
            candidate
            for candidate in candidates
            if candidate.source_id == pin.source_id
            and candidate.task_run_id == pin.source_task_run_id
        ]
        _require(
            len(matches) == 1,
            "task_source_pin_match_cardinality",
            "a task source pin must match exactly one eligible raw task stream",
            **pin.as_manifest_record(),
            eligible_candidates=[
                {
                    "source_id": candidate.source_id,
                    "source_task_run_id": candidate.task_run_id,
                }
                for candidate in candidates
            ],
        )
        selected.append((catalog_entry, matches[0]))

    selected_by_source: defaultdict[str, list[_TaskCandidate]] = defaultdict(list)
    for _, candidate in selected:
        selected_by_source[candidate.source_id].append(candidate)

    blob_stats: dict[str, _BlobClosureStats] = {}
    selected_entries: list[dict[str, Any]] = []
    stream_bytes_by_source: defaultdict[str, int] = defaultdict(int)
    for source_id, candidates in selected_by_source.items():
        refs: list[dict[str, Any]] = []
        for candidate in candidates:
            refs.extend(_validate_selected_stream(candidate))
            stream_bytes_by_source[source_id] += candidate.stream_summary.byte_count
        source_root = next(scan.root for scan in scans if scan.source_id == source_id)
        blob_stats[source_id] = _verify_blob_closure(
            source_root,
            refs,
            verify_blob_digests=verify_blob_digests,
        )

    for catalog_entry, candidate in selected:
        selected_entries.append(_selected_task_entry(catalog_entry, candidate))

    source_entries: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for scan in scans:
        chosen = selected_by_source.get(scan.source_id, [])
        stats = blob_stats.get(scan.source_id, _BlobClosureStats(0, 0, 0))
        source_entry = _source_manifest_entry(
            scan,
            selected_task_count=len(chosen),
            selected_stream_byte_count=stream_bytes_by_source[scan.source_id],
            blob_stats=stats,
        )
        source_entries.append(source_entry)
        if scan.final.get("capture_complete") is not True:
            warnings.append(
                {
                    "code": "source_run_not_globally_capture_complete",
                    "source_id": scan.source_id,
                    "message": (
                        "the source run is globally incomplete, but every selected task stream "
                        "was independently required to be capture-complete"
                    ),
                }
            )
        if scan.final.get("runtime_status") != "completed":
            warnings.append(
                {
                    "code": "source_run_runtime_not_completed",
                    "source_id": scan.source_id,
                    "runtime_status": scan.final.get("runtime_status"),
                    "message": (
                        "the source run has a non-completed global runtime status; selected task "
                        "streams were independently required to be completed"
                    ),
                }
            )

    by_source = {entry["source_id"]: entry["selected_task_count"] for entry in source_entries}
    total_blob_occurrences = sum(
        entry["selected_evidence"]["blob_reference_occurrences"] for entry in source_entries
    )
    total_unique_blobs = sum(
        entry["selected_evidence"]["unique_blob_count"] for entry in source_entries
    )
    total_unique_blob_bytes = sum(
        entry["selected_evidence"]["unique_blob_byte_count"] for entry in source_entries
    )
    selection_policy = {
        "unit": "task_run",
        "canonical_catalog_source_id": catalog_source_id,
        "candidate_resolution": (
            _PINNED_CANDIDATE_RESOLUTION if task_source_pins else _UNIQUE_CANDIDATE_RESOLUTION
        ),
        "task_runtime_status": "completed",
        "task_capture_complete": True,
        "missing_artifacts_must_be_empty": True,
        "collector_error_event_ids_must_be_empty": True,
        "task_goal_sha256_must_match_catalog": True,
        "task_outcome_score_filter": None,
        "source_run_global_capture_complete_required": False,
        "raw_events_or_blobs_copied": False,
    }
    if task_source_pins:
        selection_policy["task_source_pins"] = [
            pin.as_manifest_record() for pin in task_source_pins
        ]

    counts = {
        "task_count": len(selected_entries),
        "task_count_by_source": by_source,
        "selected_task_stream_byte_count": sum(stream_bytes_by_source.values()),
        "blob_reference_occurrences": total_blob_occurrences,
        "unique_blob_count_summed_by_source": total_unique_blobs,
        "unique_blob_byte_count_summed_by_source": total_unique_blob_bytes,
    }
    if task_source_pins:
        counts["task_source_pin_count"] = len(task_source_pins)

    manifest = {
        "schema_version": COMPOSITE_SCHEMA_VERSION,
        "artifact_type": "derived_task_selection",
        "dataset_id": dataset_id,
        "is_raw_run": False,
        "raw_schema_version": next(iter(schemas)),
        "source_locator": {
            "kind": "relative_to_external_source_base",
            "source_base_is_content_identity": False,
            "resolution_note": (
                "resolve every source relative_run_path against an explicitly supplied "
                "source base; resolve every raw BlobRef against that source run root"
            ),
        },
        "selection_policy": selection_policy,
        "canonical_catalog": {
            "task_count": len(catalog),
            "task_name_index_sha256": _sha256(
                _canonical_json_bytes(
                    [
                        {
                            "task_index": entry["canonical_suite_index"],
                            "task_name": entry["task_name"],
                        }
                        for entry in catalog
                    ]
                )
            ),
            "task_catalog_sha256": _sha256(_canonical_json_bytes(catalog)),
        },
        "sources": source_entries,
        "tasks": selected_entries,
        "selection_sha256": _sha256(_canonical_json_bytes(selected_entries)),
        "counts": counts,
    }
    checks = {
        "source_manifests_and_run_stream_hashed": True,
        "all_source_task_streams_match_final_manifest": True,
        "canonical_catalog_is_contiguous_and_unique": True,
        "selected_task_goals_match_catalog": True,
        "selected_event_stream_envelopes_and_sequences_valid": True,
        "selected_transitive_blob_paths_and_sizes_valid": True,
        "selected_transitive_blob_sha256_valid": verify_blob_digests,
        "raw_source_files_written": False,
        "raw_events_or_blobs_copied": False,
        "synthetic_run_created": False,
    }
    if task_source_pins:
        checks["every_task_resolved_by_unique_eligible_or_exact_pin"] = True
        checks["explicit_task_source_pins_matched_exactly"] = True
    else:
        checks["exactly_one_eligible_stream_per_task"] = True
    return _PreparedComposite(manifest, checks, tuple(warnings))


def _scan_source(source_id: str, root: Path, source_base: Path) -> _SourceScan:
    start_bytes, start = _read_json_object_beneath(
        root, "manifest.start.json", "manifest_start_invalid"
    )
    final_bytes, final = _read_json_object_beneath(
        root, "manifest.final.json", "manifest_final_invalid"
    )
    run_bytes = _read_regular_file_beneath(root, "run.events.jsonl", "run_events_missing")

    run_id = root.name
    _require(start.get("run_id") == run_id, "source_run_id_mismatch", "start run_id mismatch")
    _require(final.get("run_id") == run_id, "source_run_id_mismatch", "final run_id mismatch")
    raw_schema = _string(start.get("raw_schema_version"), "raw_schema_version")
    _require(
        final.get("raw_schema_version") == raw_schema,
        "source_schema_mismatch",
        "start and final manifests use different raw schema versions",
        source_id=source_id,
    )

    start_summary = _summarize_bytes(start_bytes)
    final_summary = _summarize_bytes(final_bytes)
    run_summary = _summarize_bytes(run_bytes)
    _require_summary_match(
        final.get("manifest_start"), start_summary, "manifest.start.json", source_id
    )
    _require_summary_match(final.get("run_stream"), run_summary, "run.events.jsonl", source_id)

    task_summaries = final.get("task_streams")
    _require(
        isinstance(task_summaries, list),
        "task_summaries_invalid",
        "manifest.final task_streams must be a list",
        source_id=source_id,
    )
    seen_task_run_ids: set[str] = set()
    declared_stream_paths: set[str] = set()
    candidates: list[_TaskCandidate] = []
    for summary in task_summaries:
        _require(
            isinstance(summary, dict),
            "task_summary_invalid",
            "task stream summary must be an object",
            source_id=source_id,
        )
        task_run_id = _string(summary.get("task_run_id"), "task_streams.task_run_id")
        _require(
            task_run_id not in seen_task_run_ids,
            "duplicate_task_run_id",
            "source manifest repeats a task_run_id",
            source_id=source_id,
            task_run_id=task_run_id,
        )
        seen_task_run_ids.add(task_run_id)
        relative = _safe_relative_path(
            _string(summary.get("relative_path"), "task_streams.relative_path"),
            code="task_stream_path_invalid",
        )
        _require(
            relative == PurePosixPath("tasks", task_run_id, "events.jsonl"),
            "task_stream_path_invalid",
            "task stream path must be tasks/<task_run_id>/events.jsonl",
            source_id=source_id,
            task_run_id=task_run_id,
        )
        declared_stream_paths.add(relative.as_posix())
        stream_path = root.joinpath(*relative.parts)
        stream_bytes = _read_regular_file_beneath(root, relative, "task_stream_missing")
        stream_summary = _summarize_bytes(stream_bytes)
        _require_summary_match(summary, stream_summary, str(relative), source_id)
        stream_events = tuple(_jsonl_documents(stream_bytes, stream_path))
        first, last = stream_events[0], stream_events[-1]
        _validate_endpoint_envelope(first, run_id, task_run_id, 1, "task_started")
        _validate_endpoint_envelope(
            last,
            run_id,
            task_run_id,
            _positive_int(last.get("seq"), "task_ended.seq"),
            "task_ended",
        )
        start_payload = _mapping(first.get("payload"), "task_started.payload")
        end_payload = _mapping(last.get("payload"), "task_ended.payload")
        evaluation = end_payload.get("environment_evaluation")
        evaluation_mapping = evaluation if isinstance(evaluation, Mapping) else {}
        score = evaluation_mapping.get("score")
        _require(
            score is None or (isinstance(score, (int, float)) and not isinstance(score, bool)),
            "task_score_invalid",
            "task score must be numeric or null",
            source_id=source_id,
            task_run_id=task_run_id,
        )
        reason = evaluation_mapping.get("reason")
        _require(
            reason is None or isinstance(reason, str),
            "task_reason_invalid",
            "task reason must be a string or null",
            source_id=source_id,
            task_run_id=task_run_id,
        )
        candidates.append(
            _TaskCandidate(
                source_id=source_id,
                source_root=root,
                source_run_id=run_id,
                raw_schema_version=raw_schema,
                task_run_id=task_run_id,
                stream_relative_path=str(relative),
                stream_path=stream_path,
                stream_summary=stream_summary,
                stream_events=stream_events,
                summary_runtime_status=_optional_string(summary.get("runtime_status")),
                summary_capture_complete=summary.get("capture_complete"),
                summary_missing_artifacts=_string_tuple(summary.get("missing_artifacts")),
                summary_collector_error_event_ids=_string_tuple(
                    summary.get("collector_error_event_ids")
                ),
                task_name=_string(start_payload.get("task_name"), "task_started.task_name"),
                source_task_index=_positive_int(
                    start_payload.get("task_index"), "task_started.task_index"
                ),
                whole_task_attempt_index=_positive_int(
                    start_payload.get("whole_task_attempt_index"),
                    "task_started.whole_task_attempt_index",
                ),
                task_goal=_string(start_payload.get("task_goal"), "task_started.task_goal"),
                task_started_event_id=_string(first.get("event_id"), "task_started.event_id"),
                task_ended_event_id=_string(last.get("event_id"), "task_ended.event_id"),
                runtime_status=_optional_string(end_payload.get("runtime_status")),
                capture_complete=end_payload.get("capture_complete"),
                missing_artifacts=_string_tuple(end_payload.get("missing_artifacts")),
                collector_error_event_ids=_string_tuple(
                    end_payload.get("collector_error_event_ids")
                ),
                environment_score=score,
                environment_reason=reason,
            )
        )

    physical_stream_paths = _enumerate_physical_task_streams(root, source_id)
    _require(
        physical_stream_paths == declared_stream_paths,
        "physical_task_layout_mismatch",
        "physical tasks/*/events.jsonl files must exactly match manifest.final task_streams",
        source_id=source_id,
        undeclared=sorted(physical_stream_paths - declared_stream_paths),
        missing=sorted(declared_stream_paths - physical_stream_paths),
    )

    return _SourceScan(
        source_id=source_id,
        root=root,
        relative_run_path=root.relative_to(source_base).as_posix(),
        run_id=run_id,
        raw_schema_version=raw_schema,
        start=start,
        final=final,
        manifest_start=start_summary,
        manifest_final=final_summary,
        run_events=run_summary,
        candidates=tuple(candidates),
    )


def _enumerate_physical_task_streams(root: Path, source_id: str) -> set[str]:
    tasks_root = root / "tasks"
    try:
        root_metadata = tasks_root.lstat()
    except FileNotFoundError as error:
        raise CompositeBuildError(
            "physical_tasks_missing",
            "source run has no physical tasks directory",
            source_id=source_id,
            path=str(tasks_root),
        ) from error
    _require(
        stat.S_ISDIR(root_metadata.st_mode) and not stat.S_ISLNK(root_metadata.st_mode),
        "physical_task_path_unsafe",
        "physical tasks directory must be a non-symlink directory",
        source_id=source_id,
        path=str(tasks_root),
    )
    physical: set[str] = set()
    try:
        with os.scandir(tasks_root) as task_entries:
            for task_entry in task_entries:
                _require(
                    not task_entry.is_symlink() and task_entry.is_dir(follow_symlinks=False),
                    "physical_task_path_unsafe",
                    "every physical tasks child must be a non-symlink directory",
                    source_id=source_id,
                    path=task_entry.path,
                )
                with os.scandir(task_entry.path) as stream_entries:
                    children = list(stream_entries)
                _require(
                    len(children) == 1
                    and children[0].name == "events.jsonl"
                    and not children[0].is_symlink()
                    and children[0].is_file(follow_symlinks=False),
                    "physical_task_layout_invalid",
                    "each physical task directory must contain only a regular events.jsonl",
                    source_id=source_id,
                    path=task_entry.path,
                )
                physical.add(PurePosixPath("tasks", task_entry.name, "events.jsonl").as_posix())
    except CompositeBuildError:
        raise
    except OSError as error:
        raise CompositeBuildError(
            "physical_task_enumeration_failed",
            "failed to enumerate physical task streams securely",
            source_id=source_id,
            path=str(tasks_root),
            errno=error.errno,
        ) from error
    return physical


def _build_catalog(scan: _SourceScan, expected_task_count: int) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    by_index: dict[int, str] = {}
    for candidate in scan.candidates:
        goal_hash = _sha256(candidate.task_goal.encode("utf-8"))
        entry = {
            "canonical_suite_index": candidate.source_task_index,
            "task_name": candidate.task_name,
            "task_goal_utf8_sha256": goal_hash,
            "task_goal_utf8_byte_count": len(candidate.task_goal.encode("utf-8")),
        }
        existing = by_name.setdefault(candidate.task_name, entry)
        _require(
            existing == entry,
            "catalog_task_inconsistent",
            "canonical source retries disagree on task index or task goal",
            task_name=candidate.task_name,
        )
        existing_name = by_index.setdefault(candidate.source_task_index, candidate.task_name)
        _require(
            existing_name == candidate.task_name,
            "catalog_index_duplicate",
            "canonical source maps one task index to multiple names",
            task_index=candidate.source_task_index,
        )
    catalog = sorted(by_name.values(), key=lambda item: item["canonical_suite_index"])
    _require(
        len(catalog) == expected_task_count,
        "catalog_task_count_mismatch",
        "canonical catalog task count differs from the requested count",
        expected=expected_task_count,
        actual=len(catalog),
    )
    _require(
        [entry["canonical_suite_index"] for entry in catalog]
        == list(range(1, expected_task_count + 1)),
        "catalog_indices_not_contiguous",
        "canonical task indices must be unique and contiguous from one",
    )
    return catalog


def _validate_selected_stream(candidate: _TaskCandidate) -> list[dict[str, Any]]:
    events = candidate.stream_events
    refs: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    task_started_positions: list[int] = []
    task_ended_positions: list[int] = []
    collector_error_event_ids: list[str] = []
    for expected_seq, event in enumerate(events, start=1):
        try:
            validate_event_envelope(event)
        except (SchemaValidationError, TypeError, ValueError) as error:
            raise CompositeBuildError(
                "selected_stream_event_schema_invalid",
                "selected task event violates the authoritative v1 common envelope",
                source_id=candidate.source_id,
                task_run_id=candidate.task_run_id,
                seq=expected_seq,
                schema_error=str(error),
            ) from error
        _require(
            event["schema_version"] == candidate.raw_schema_version,
            "selected_stream_event_schema_invalid",
            "selected event schema differs from its source manifest schema",
            source_id=candidate.source_id,
            task_run_id=candidate.task_run_id,
            seq=expected_seq,
        )
        _require(
            event.get("seq") == expected_seq,
            "selected_stream_seq_invalid",
            "selected task stream seq must be contiguous from one",
            source_id=candidate.source_id,
            task_run_id=candidate.task_run_id,
            expected_seq=expected_seq,
            actual_seq=event.get("seq"),
        )
        _require(
            event.get("run_id") == candidate.source_run_id
            and event.get("task_run_id") == candidate.task_run_id
            and event.get("stream_id") == candidate.task_run_id,
            "selected_stream_identity_invalid",
            "selected event envelope does not match its physical source stream",
            source_id=candidate.source_id,
            task_run_id=candidate.task_run_id,
            seq=expected_seq,
        )
        event_id = _string(event.get("event_id"), "event.event_id")
        _require(
            event_id not in seen_event_ids,
            "selected_stream_event_id_duplicate",
            "selected task stream repeats an event_id",
            task_run_id=candidate.task_run_id,
            event_id=event_id,
        )
        seen_event_ids.add(event_id)
        if event["event_type"] == "task_started":
            task_started_positions.append(expected_seq)
        elif event["event_type"] == "task_ended":
            task_ended_positions.append(expected_seq)
        elif event["event_type"] == "collector_error":
            collector_error_event_ids.append(event_id)
        refs.extend(_iter_blob_refs(event))
    _require(
        task_started_positions == [1] and task_ended_positions == [len(events)],
        "selected_stream_lifecycle_invalid",
        "selected task stream must contain one task_started first and one task_ended last",
        task_run_id=candidate.task_run_id,
        task_started_positions=task_started_positions,
        task_ended_positions=task_ended_positions,
    )
    actual_collector_errors = tuple(collector_error_event_ids)
    _require(
        actual_collector_errors
        == candidate.collector_error_event_ids
        == candidate.summary_collector_error_event_ids,
        "selected_stream_collector_errors_mismatch",
        "collector_error events, task_ended, and final summary must list identical IDs",
        task_run_id=candidate.task_run_id,
        event_ids=list(actual_collector_errors),
        task_ended_ids=list(candidate.collector_error_event_ids),
        summary_ids=list(candidate.summary_collector_error_event_ids),
    )
    return refs


def _verify_blob_closure(
    source_root: Path,
    initial_refs: Iterable[dict[str, Any]],
    *,
    verify_blob_digests: bool,
) -> _BlobClosureStats:
    queue: deque[dict[str, Any]] = deque(initial_refs)
    occurrences = 0
    verified: dict[str, tuple[Any, ...]] = {}
    media_types_by_path: defaultdict[str, set[str]] = defaultdict(set)
    parsed_json_paths: set[str] = set()
    total_bytes = 0
    while queue:
        reference = queue.popleft()
        occurrences += 1
        _validate_blob_ref_shape(reference)
        relative = _safe_relative_path(reference["relative_path"], code="blob_path_invalid")
        digest = reference["digest"]
        expected = PurePosixPath("blobs", "sha256", digest[:2], digest)
        _require(
            relative == expected,
            "blob_path_invalid",
            "BlobRef path must match its sha256 digest",
            relative_path=str(relative),
            digest=digest,
        )
        relative_key = str(relative)
        content_identity = (
            reference["algorithm"],
            digest,
            reference["byte_length"],
            relative_key,
        )
        media_type = reference["media_type"]
        media_types_by_path[relative_key].add(media_type)
        is_json = media_type.endswith("/json") or media_type.endswith("+json")
        prior = verified.get(relative_key)
        if prior is not None:
            _require(
                prior == content_identity,
                "blob_reference_conflict",
                "the same blob path has conflicting physical identity fields",
                relative_path=relative_key,
            )
            if not is_json or relative_key in parsed_json_paths:
                continue

        blob_bytes = _read_regular_file_beneath(source_root, relative, "blob_missing")
        actual_size = len(blob_bytes)
        _require(
            actual_size == reference["byte_length"],
            "blob_size_mismatch",
            "BlobRef byte_length does not match the stored file",
            relative_path=relative_key,
            expected=reference["byte_length"],
            actual=actual_size,
        )
        if verify_blob_digests:
            _require(
                _sha256(blob_bytes) == digest,
                "blob_digest_mismatch",
                "selected blob does not match its sha256 digest",
                relative_path=relative_key,
            )
        if prior is None:
            verified[relative_key] = content_identity
            total_bytes += actual_size
        if is_json and relative_key not in parsed_json_paths:
            try:
                graph = _loads_strict(blob_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise CompositeBuildError(
                    "json_blob_invalid",
                    "selected JSON blob cannot be parsed while traversing transitive references",
                    relative_path=relative_key,
                ) from error
            parsed_json_paths.add(relative_key)
            queue.extend(_iter_blob_refs(graph))
    return _BlobClosureStats(occurrences, len(verified), total_bytes)


def _selected_task_entry(
    catalog_entry: Mapping[str, Any], candidate: _TaskCandidate
) -> dict[str, Any]:
    reason = candidate.environment_reason
    reason_bytes = reason.encode("utf-8") if reason is not None else None
    return {
        "canonical_suite_index": catalog_entry["canonical_suite_index"],
        "task_name": candidate.task_name,
        "task_goal_utf8_sha256": _sha256(candidate.task_goal.encode("utf-8")),
        "task_goal_utf8_byte_count": len(candidate.task_goal.encode("utf-8")),
        "source_id": candidate.source_id,
        "source_run_id": candidate.source_run_id,
        "source_task_run_id": candidate.task_run_id,
        "source_task_index": candidate.source_task_index,
        "whole_task_attempt_index": candidate.whole_task_attempt_index,
        "task_stream": {
            "relative_path": candidate.stream_relative_path,
            "sha256": candidate.stream_summary.sha256,
            "byte_count": candidate.stream_summary.byte_count,
        },
        "task_started_event_id": candidate.task_started_event_id,
        "task_ended_event_id": candidate.task_ended_event_id,
        "runtime_status": candidate.runtime_status,
        "capture_complete": candidate.capture_complete,
        "missing_artifacts": list(candidate.missing_artifacts),
        "collector_error_event_ids": list(candidate.collector_error_event_ids),
        "environment_evaluation": {
            "score": candidate.environment_score,
            "reason_utf8_sha256": _sha256(reason_bytes) if reason_bytes is not None else None,
            "reason_utf8_byte_count": len(reason_bytes) if reason_bytes is not None else None,
        },
    }


def _source_manifest_entry(
    scan: _SourceScan,
    *,
    selected_task_count: int,
    selected_stream_byte_count: int,
    blob_stats: _BlobClosureStats,
) -> dict[str, Any]:
    file_identity = {
        "run_id": scan.run_id,
        "manifest_start_sha256": scan.manifest_start.sha256,
        "manifest_final_sha256": scan.manifest_final.sha256,
        "run_events_sha256": scan.run_events.sha256,
    }
    return {
        "source_id": scan.source_id,
        "run_id": scan.run_id,
        "relative_run_path": scan.relative_run_path,
        "raw_schema_version": scan.raw_schema_version,
        "source_run_runtime_status": scan.final.get("runtime_status"),
        "source_run_capture_complete": scan.final.get("capture_complete"),
        "source_run_missing_artifacts": scan.final.get("missing_artifacts"),
        "source_run_collector_error_event_ids": scan.final.get("collector_error_event_ids"),
        "manifest_start": scan.manifest_start.as_dict("manifest.start.json"),
        "manifest_final": scan.manifest_final.as_dict("manifest.final.json"),
        "run_events": scan.run_events.as_dict("run.events.jsonl"),
        "source_fingerprint_sha256": _sha256(_canonical_json_bytes(file_identity)),
        "provenance": {
            "git_commit": scan.start.get("git_commit"),
            "git_dirty": scan.start.get("git_dirty"),
            "git_dirty_status": scan.start.get("git_dirty_status"),
            "agent_type": scan.start.get("agent_type"),
            "model_name": scan.start.get("model_name"),
            "environment_image": scan.start.get("environment_image"),
            "started_at_utc": scan.start.get("started_at_utc"),
        },
        "selected_task_count": selected_task_count,
        "selected_evidence": {
            "task_stream_byte_count": selected_stream_byte_count,
            "blob_reference_occurrences": blob_stats.reference_occurrences,
            "unique_blob_count": blob_stats.unique_blob_count,
            "unique_blob_byte_count": blob_stats.unique_blob_byte_count,
        },
    }


def _validation_report(
    *,
    manifest: Mapping[str, Any],
    manifest_bytes: bytes,
    source_base: Path,
    checks: Mapping[str, bool],
    warnings: Sequence[Mapping[str, Any]],
    verify_blob_digests: bool,
) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "checker_version": CHECKER_VERSION,
        "checked_at_utc": _utc_now(),
        "valid": True,
        "verification_mode": (
            "selected_streams_and_transitive_blob_sha256"
            if verify_blob_digests
            else "selected_streams_and_transitive_blob_size"
        ),
        "manifest_sha256": _sha256(manifest_bytes),
        "dataset_id": manifest["dataset_id"],
        "source_base_resolved": str(source_base),
        "errors": [],
        "warnings": [dict(warning) for warning in warnings],
        "checks": dict(checks),
        "counts": dict(manifest["counts"]),
    }


def _resolve_sources(
    source_base: Path, sources: Mapping[str, str | os.PathLike[str]]
) -> dict[str, Path]:
    _require(bool(sources), "sources_missing", "at least one source run is required")
    resolved: dict[str, Path] = {}
    seen_roots: set[Path] = set()
    for source_id in sorted(sources):
        requested = sources[source_id]
        _require(
            bool(_SOURCE_ID_RE.fullmatch(source_id)),
            "source_id_invalid",
            "source ID must be lowercase and filesystem-neutral",
            source_id=source_id,
        )
        candidate = Path(requested)
        path = candidate if candidate.is_absolute() else source_base / candidate
        root = _resolve_directory_beneath(
            source_base,
            path,
            code="source_run_missing",
            source_id=source_id,
        )
        _require(
            root not in seen_roots,
            "duplicate_source_root",
            "one physical run cannot be supplied under multiple source IDs",
            source_id=source_id,
        )
        seen_roots.add(root)
        resolved[source_id] = root
    return resolved


def _validate_output_location(output: Path, source_roots: Iterable[Path]) -> None:
    for source_root in source_roots:
        _require(
            not output.is_relative_to(source_root),
            "output_inside_source_run",
            "derived output must not be written inside a raw source run",
            source_run=str(source_root),
        )
        raw_root = _raw_root_for_run(source_root)
        if raw_root is not None:
            _require(
                not output.is_relative_to(raw_root),
                "output_inside_raw_root",
                "derived output must not be written anywhere under a source raw root",
                raw_root=str(raw_root),
            )


def _raw_root_for_run(run_root: Path) -> Path | None:
    for parent in run_root.parents:
        if parent.name == "runs" and run_root.is_relative_to(parent):
            return parent.parent
    return None


def _resolve_existing_directory(path: Path, *, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise CompositeBuildError(
            code, "required directory does not exist", path=str(path)
        ) from error
    _require(resolved.is_dir(), code, "required path is not a directory", path=str(resolved))
    return resolved


def _resolve_directory_beneath(
    root: Path,
    path: Path,
    *,
    code: str,
    **context: Any,
) -> Path:
    """Resolve a directory below ``root`` without accepting symlink components."""

    _require(
        path.is_absolute() and ".." not in path.parts,
        "source_outside_base",
        "source path must be a normalized absolute path below source_base",
        path=str(path),
        **context,
    )
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise CompositeBuildError(
            "source_outside_base",
            "every source run must be located below source_base",
            path=str(path),
            **context,
        ) from error
    _require(
        bool(relative.parts),
        "source_outside_base",
        "a source run cannot be source_base itself",
        path=str(path),
        **context,
    )
    current = root
    try:
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            _require(
                not stat.S_ISLNK(metadata.st_mode),
                "source_path_symlink",
                "source paths must not contain symlink components",
                path=str(current),
                **context,
            )
    except FileNotFoundError as error:
        raise CompositeBuildError(
            code, "required directory does not exist", path=str(path)
        ) from error
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise CompositeBuildError(
            code, "required directory does not exist", path=str(path)
        ) from error
    _require(
        resolved.is_relative_to(root),
        "source_outside_base",
        "resolved source directory escapes source_base",
        path=str(path),
        resolved=str(resolved),
        **context,
    )
    _require(resolved.is_dir(), code, "required path is not a directory", path=str(resolved))
    return resolved


def _resolve_output_path(path: Path) -> Path:
    try:
        parent = path.parent.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise CompositeBuildError(
            "output_parent_missing", "output directory parent must already exist", path=str(path)
        ) from error
    return parent / path.name


def _resolve_existing_regular_file(path: Path, *, code: str) -> Path:
    _require(not path.is_symlink(), code, "required file must not be a symlink", path=str(path))
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise CompositeBuildError(code, "required file does not exist", path=str(path)) from error
    _require(resolved.is_file(), code, "required path is not a regular file", path=str(resolved))
    return resolved


def _source_specs_from_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    sources = manifest.get("sources")
    _require(isinstance(sources, list) and sources, "sources_invalid", "sources must be a list")
    result: dict[str, str] = {}
    for source in sources:
        mapping = _mapping(source, "sources[]")
        source_id = _string(mapping.get("source_id"), "sources[].source_id")
        relative_path = _string(mapping.get("relative_run_path"), "sources[].relative_run_path")
        _require(
            source_id not in result,
            "source_id_duplicate",
            "manifest repeats a source ID",
            source_id=source_id,
        )
        result[source_id] = relative_path
    return result


def _validate_manifest_header(manifest: Mapping[str, Any]) -> None:
    _require(
        manifest.get("schema_version") == COMPOSITE_SCHEMA_VERSION,
        "manifest_schema_invalid",
        "unsupported composite manifest schema",
    )
    _require(
        manifest.get("artifact_type") == "derived_task_selection"
        and manifest.get("is_raw_run") is False,
        "manifest_artifact_type_invalid",
        "composite must explicitly identify itself as a non-raw derived task selection",
    )


def _validate_dataset_id(value: Any) -> str:
    dataset_id = _string(value, "dataset_id")
    _require(
        bool(_DATASET_ID_RE.fullmatch(dataset_id)),
        "dataset_id_invalid",
        "dataset_id must contain only letters, digits, dots, underscores, and hyphens",
        dataset_id=dataset_id,
    )
    return dataset_id


def _read_json_object_beneath(
    root: Path, relative_path: str | PurePosixPath, code: str
) -> tuple[bytes, dict[str, Any]]:
    data = _read_regular_file_beneath(root, relative_path, code)
    display_path = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        value = _loads_strict(data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CompositeBuildError(
            code, "JSON document is invalid", path=str(display_path)
        ) from error
    _require(
        isinstance(value, dict), code, "JSON document must be an object", path=str(display_path)
    )
    return data, value


def _read_regular_file(path: Path, code: str) -> bytes:
    _require_regular_nonsymlink(path, code)
    try:
        return path.read_bytes()
    except OSError as error:
        raise CompositeBuildError(code, "failed to read required file", path=str(path)) from error


def _read_regular_file_beneath(
    root: Path,
    relative_path: str | PurePosixPath,
    code: str,
) -> bytes:
    """Read once through an O_NOFOLLOW dirfd walk rooted at an immutable run."""

    relative = (
        _safe_relative_path(relative_path, code=code)
        if isinstance(relative_path, str)
        else relative_path
    )
    _require(
        not relative.is_absolute()
        and bool(relative.parts)
        and ".." not in relative.parts
        and "." not in relative.parts,
        code,
        "evidence path must be a safe relative path",
        relative_path=str(relative),
    )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    _require(
        nofollow is not None,
        "secure_path_walk_unsupported",
        "this platform does not support O_NOFOLLOW",
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    display_path = root.joinpath(*relative.parts)
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)
        descriptors.append(file_descriptor)
        metadata = os.fstat(file_descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode),
            code,
            "required evidence must be a regular file",
            path=str(display_path),
        )
        chunks: list[bytes] = []
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except CompositeBuildError:
        raise
    except OSError as error:
        raise CompositeBuildError(
            code,
            "failed secure read; a path component may be missing or a symlink",
            path=str(display_path),
            errno=error.errno,
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_regular_nonsymlink(path: Path, code: str) -> None:
    _require(
        not path.is_symlink() and path.is_file(),
        code,
        "required evidence must be a regular non-symlink file",
        path=str(path),
    )


def _jsonl_documents(data: bytes, path: Path) -> list[dict[str, Any]]:
    _require(data.endswith(b"\n"), "jsonl_truncated", "JSONL must end in a newline", path=str(path))
    raw_lines = data.splitlines()
    _require(bool(raw_lines), "jsonl_empty", "JSONL stream must not be empty", path=str(path))
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw_lines, start=1):
        _require(
            bool(line), "jsonl_blank_line", "JSONL must not contain blank lines", path=str(path)
        )
        try:
            value = _loads_strict(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CompositeBuildError(
                "jsonl_invalid", "JSONL line is invalid", path=str(path), line=line_number
            ) from error
        _require(
            isinstance(value, dict),
            "jsonl_event_shape_invalid",
            "every JSONL line must be an object",
            path=str(path),
            line=line_number,
        )
        events.append(value)
    return events


def _validate_endpoint_envelope(
    event: Mapping[str, Any],
    run_id: str,
    task_run_id: str,
    seq: int,
    event_type: str,
) -> None:
    _require(
        event.get("event_type") == event_type
        and event.get("run_id") == run_id
        and event.get("task_run_id") == task_run_id
        and event.get("stream_id") == task_run_id
        and event.get("seq") == seq,
        "task_stream_endpoint_invalid",
        "task stream lifecycle endpoint does not match its source identity",
        task_run_id=task_run_id,
        event_type=event_type,
    )


def _iter_blob_refs(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if _BLOB_FIELDS <= value.keys():
            _require(
                set(value) == _BLOB_FIELDS,
                "blob_ref_shape_invalid",
                "BlobRef must contain exactly the contract fields",
            )
            yield value
            return
        for child in value.values():
            yield from _iter_blob_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_blob_refs(child)


def _validate_blob_ref_shape(reference: Mapping[str, Any]) -> None:
    _require(
        reference.get("algorithm") == "sha256"
        and isinstance(reference.get("digest"), str)
        and _SHA256_RE.fullmatch(reference["digest"]) is not None
        and isinstance(reference.get("byte_length"), int)
        and not isinstance(reference.get("byte_length"), bool)
        and reference["byte_length"] >= 0
        and isinstance(reference.get("media_type"), str)
        and bool(reference["media_type"])
        and isinstance(reference.get("relative_path"), str),
        "blob_ref_shape_invalid",
        "BlobRef fields are invalid",
    )


def _safe_relative_path(value: str, *, code: str) -> PurePosixPath:
    path = PurePosixPath(value)
    _require(
        value == path.as_posix()
        and not path.is_absolute()
        and bool(path.parts)
        and ".." not in path.parts
        and "." not in path.parts,
        code,
        "path must be a normalized safe POSIX-relative path",
        relative_path=value,
    )
    return path


def _require_summary_match(declared: Any, actual: _FileSummary, name: str, source_id: str) -> None:
    _require(
        isinstance(declared, Mapping)
        and declared.get("sha256") == actual.sha256
        and declared.get("byte_count") == actual.byte_count,
        "source_file_summary_mismatch",
        "source final manifest file summary does not match stored bytes",
        source_id=source_id,
        file=name,
    )


def _summarize_bytes(data: bytes) -> _FileSummary:
    return _FileSummary(_sha256(data), len(data))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _loads_strict(data: bytes) -> Any:
    return json.loads(data, object_pairs_hook=_reject_duplicate_keys)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _canonical_json_bytes(value: Any, *, newline: bool = False) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_path_noreplace(source: Path, target: Path) -> None:
    """Atomically rename without replacing any target, including an empty directory."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    _require(
        renameat2 is not None,
        "atomic_publish_unsupported",
        "libc renameat2(RENAME_NOREPLACE) is required for exclusive publication",
    )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise CompositeBuildError(
            "output_exists",
            "exclusive publication refused an existing target",
            output_path=str(target),
        )
    raise CompositeBuildError(
        "atomic_publish_failed",
        "renameat2(RENAME_NOREPLACE) failed",
        source=str(source),
        target=str(target),
        errno=error_number,
        error=os.strerror(error_number),
    )


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    _rename_path_noreplace(source, target)


def _atomic_write_noreplace(path: Path, data: bytes) -> None:
    _require(
        path.parent.is_dir(),
        "output_parent_missing",
        "output file parent must already exist",
        output_parent=str(path.parent),
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.staging-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _rename_path_noreplace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), "field_shape_invalid", f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    _require(
        isinstance(value, str) and bool(value),
        "field_shape_invalid",
        f"{field} must be a non-empty string",
    )
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _positive_int(value: Any, field: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        "field_shape_invalid",
        f"{field} must be a positive integer",
    )
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ("<invalid-shape>",)
    if not all(isinstance(item, str) for item in value):
        return ("<invalid-shape>",)
    return tuple(value)


def _require(condition: bool, code: str, message: str, **context: Any) -> None:
    if not condition:
        raise CompositeBuildError(code, message, **context)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_source_specs(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        source_id, separator, path = value.partition("=")
        if not separator or not source_id or not path:
            raise CompositeBuildError(
                "source_argument_invalid", "--source must have the form SOURCE_ID=RUN_PATH"
            )
        if source_id in result:
            raise CompositeBuildError(
                "source_id_duplicate", "--source repeats a source ID", source_id=source_id
            )
        result[source_id] = path
    return result


def _parse_task_source_pin_specs(values: Sequence[str]) -> tuple[TaskSourcePin, ...]:
    pins: list[TaskSourcePin] = []
    for value in values:
        canonical, task_separator, source = value.partition("=")
        index_text, index_separator, task_name = canonical.partition(":")
        source_id, source_separator, source_task_run_id = source.partition(":")
        if (
            not task_separator
            or not index_separator
            or not source_separator
            or not index_text
            or not task_name
            or not source_id
            or not source_task_run_id
            or ":" in task_name
            or ":" in source_task_run_id
        ):
            raise CompositeBuildError(
                "task_source_pin_argument_invalid",
                (
                    "--task-source-pin must have the form "
                    "CANONICAL_INDEX:TASK_NAME=SOURCE_ID:TASK_RUN_ID"
                ),
                argument=value,
            )
        try:
            index = int(index_text)
        except ValueError as error:
            raise CompositeBuildError(
                "task_source_pin_argument_invalid",
                "--task-source-pin CANONICAL_INDEX must be a positive integer",
                argument=value,
            ) from error
        pins.append(
            TaskSourcePin(
                canonical_suite_index=index,
                task_name=task_name,
                source_id=source_id,
                source_task_run_id=source_task_run_id,
            )
        )
    return _normalize_task_source_pins(pins)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify a zero-copy MobileWorld curated task-set manifest."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="validate sources and create a derived manifest")
    build.add_argument("--source-base", type=Path, required=True)
    build.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="SOURCE_ID=RUN_PATH",
        help="repeat for each source; RUN_PATH may be relative to --source-base",
    )
    build.add_argument("--catalog-source", required=True)
    build.add_argument("--expected-task-count", type=int, required=True)
    build.add_argument(
        "--task-source-pin",
        action="append",
        default=[],
        metavar="CANONICAL_INDEX:TASK_NAME=SOURCE_ID:TASK_RUN_ID",
        help=(
            "repeat to select an exact eligible raw stream for a canonical task; "
            "unpinned tasks must still have exactly one eligible stream"
        ),
    )
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--dataset-id")
    build.add_argument(
        "--verify-blob-digests",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="hash every transitively referenced selected blob (default: enabled)",
    )

    validate = subparsers.add_parser("validate", help="independently verify a manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--source-base", type=Path, required=True)
    validate.add_argument("--report", type=Path)
    validate.add_argument(
        "--verify-blob-digests",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            sources = _parse_source_specs(args.source)
            task_source_pins = _parse_task_source_pin_specs(args.task_source_pin)
            manifest_path, report_path, manifest, report = build_curated_composite(
                source_base=args.source_base,
                sources=sources,
                catalog_source_id=args.catalog_source,
                expected_task_count=args.expected_task_count,
                output_dir=args.output_dir,
                dataset_id=args.dataset_id,
                verify_blob_digests=args.verify_blob_digests,
                task_source_pins=task_source_pins,
            )
            result = {
                "valid": report["valid"],
                "dataset_id": manifest["dataset_id"],
                "task_count": manifest["counts"]["task_count"],
                "manifest": str(manifest_path),
                "manifest_sha256": report["manifest_sha256"],
                "validation_report": str(report_path),
            }
        else:
            report = validate_curated_composite(
                manifest_path=args.manifest,
                source_base=args.source_base,
                verify_blob_digests=args.verify_blob_digests,
            )
            if args.report is not None:
                write_validation_report(
                    report=report,
                    report_path=args.report,
                    manifest_path=args.manifest,
                    source_base=args.source_base,
                )
            result = report
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("valid") is True else 1
    except CompositeBuildError as error:
        print(
            json.dumps(
                {"valid": False, "error": error.code, "message": str(error), **error.context},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
