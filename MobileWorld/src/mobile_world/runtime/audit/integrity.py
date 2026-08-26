"""Offline integrity validation for one immutable raw audit run."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_world.runtime.audit.blob_store import BlobIntegrityError, BlobRef, BlobStore
from mobile_world.runtime.audit.schemas import (
    SCHEMA_VERSION,
    SchemaValidationError,
    validate_collector_metadata_keys,
    validate_event_envelope,
)
from mobile_world.runtime.audit.secret_policy import is_placeholder_credential
from mobile_world.runtime.audit.serializer import (
    ARTIFACT_GRAPH_MEDIA_TYPE,
    ArtifactSerializer,
    SerializationError,
)

CHECKER_VERSION = "mobileworld.audit.integrity/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_START_MANIFEST_REQUIRED = frozenset(
    {
        "raw_schema_version",
        "run_id",
        "repository",
        "git_commit",
        "git_dirty",
        "python_version",
        "mobile_world_version",
        "agent_type",
        "model_name",
        "suite_family",
        "resolved_cli_config",
        "resolved_agent_runtime_config",
        "environment_image",
        "started_at_utc",
        "collection_policy",
    }
)
_FINAL_MANIFEST_REQUIRED = frozenset(
    {
        "raw_schema_version",
        "run_id",
        "ended_at_utc",
        "runtime_status",
        "manifest_start",
        "run_stream",
        "task_streams",
        "blob_count",
        "blob_byte_count",
        "capture_complete",
        "missing_artifacts",
        "collector_error_event_ids",
    }
)
_TASK_STARTED_REQUIRED = frozenset(
    {
        "task_name",
        "task_goal",
        "task_goal_status",
        "task_index",
        "suite_family",
        "agent",
        "environment",
        "whole_task_attempt_index",
    }
)
_TASK_ENDED_REQUIRED = frozenset(
    {
        "runtime_status",
        "termination",
        "environment_evaluation",
        "teardown",
        "token_usage",
        "capture_complete",
        "missing_artifacts",
        "collector_error_event_ids",
    }
)
_RUN_ENDED_REQUIRED = frozenset(
    {
        "runtime_status",
        "task_run_ids",
        "task_counts",
        "capture_complete",
        "collector_error_event_ids",
        "manifest_final_path",
    }
)
_TASK_SUMMARY_REQUIRED = frozenset(
    {
        "task_run_id",
        "relative_path",
        "sha256",
        "byte_count",
        "runtime_status",
        "retry_planned",
        "capture_complete",
        "missing_artifacts",
        "collector_error_event_ids",
    }
)
_RUNTIME_STATUSES = frozenset({"completed", "aborted", "crashed"})
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "authorization_header",
        "authorization_headers",
        "bearer_token",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "access_token",
        "refresh_token",
        "set_cookie",
        "client_secret",
        "secret",
        "x_api_key",
    }
)
_BLOB_FIELDS = frozenset({"algorithm", "digest", "byte_length", "media_type", "relative_path"})
_EVENT_OPAQUE_CREDENTIAL_KEYS: dict[str, frozenset[str]] = {
    "task_started": frozenset({"task_goal"}),
    "step_started": frozenset({"observation"}),
    "adapter_state_snapshot": frozenset({"state_view"}),
    "model_request": frozenset({"request_view"}),
    "model_stream_chunk": frozenset({"chunk_view"}),
    "model_response": frozenset({"raw_response_view", "normalized_response"}),
    "model_attempt_failed": frozenset({"normalized_partial_response"}),
    "agent_decision": frozenset({"prediction_raw", "parsed_action", "parse_exception"}),
    "action_execution_started": frozenset({"action"}),
    "transition_completed": frozenset(
        {"action", "post_observation", "agent_visible_tool_result", "ask_user_response"}
    ),
    "transition_failed": frozenset(
        {"action", "post_observation", "agent_visible_tool_result", "ask_user_response"}
    ),
    "transition_not_executed": frozenset({"action", "post_observation"}),
}
_EVENT_OPAQUE_LABEL_KEYS = frozenset(
    {
        "task_goal",
        "observation",
        "state_view",
        "request_view",
        "chunk_view",
        "raw_response_view",
        "normalized_response",
        "normalized_partial_response",
        "prediction_raw",
        "parsed_action",
        "parse_exception",
        "action",
        "post_observation",
        "agent_visible_tool_result",
        "ask_user_response",
        "response_headers",
    }
)


@dataclass(frozen=True, slots=True)
class _EventRecord:
    event: dict[str, Any]
    source: Path
    line: int
    expected_task_run_id: str | None

    @property
    def event_id(self) -> str | None:
        value = self.event.get("event_id")
        return value if isinstance(value, str) else None

    @property
    def task_run_id(self) -> str | None:
        value = self.event.get("task_run_id")
        return value if isinstance(value, str) else self.expected_task_run_id

    @property
    def event_type(self) -> str | None:
        value = self.event.get("event_type")
        return value if isinstance(value, str) else None

    @property
    def seq(self) -> int | None:
        value = self.event.get("seq")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @property
    def payload(self) -> Mapping[str, Any]:
        value = self.event.get("payload")
        return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class _BlobOccurrence:
    reference: dict[str, Any]
    source: Path
    json_path: str
    task_run_id: str | None


class _DuplicateJsonKey(ValueError):
    pass


class IntegrityChecker:
    """Validate event, graph, blob, manifest, and secret invariants."""

    def __init__(
        self,
        run_root: str | os.PathLike[str],
        *,
        configured_secrets: Iterable[str | bytes] = (),
    ) -> None:
        self.run_root = Path(run_root)
        self.blob_store = BlobStore(self.run_root)
        self.artifact_serializer = ArtifactSerializer(self.blob_store)
        self._secrets = _normalize_secrets(configured_secrets)
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self._records: list[_EventRecord] = []
        self._manifests: dict[str, dict[str, Any]] = {}
        self._blob_occurrences: list[_BlobOccurrence] = []
        self._verified_blob_paths: set[str] = set()
        self._secret_hits: set[tuple[str, str]] = set()

    def check(self) -> dict[str, Any]:
        """Run all checks and return a JSON-serializable report."""

        self._reset()
        self._load_manifests()
        self._load_event_streams()
        self._validate_event_envelopes_and_order()
        self._validate_lifecycle()
        self._validate_required_event_payloads()
        self._validate_model_attempts()
        self._validate_transitions()
        self._validate_manifest_summaries()
        self._collect_and_validate_blobs()
        self._scan_all_evidence_for_secrets()
        self._validate_capture_complete()
        self._finish_counts()
        return {
            "valid": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": self.counts,
            "checked_at": _utc_now(),
            "checker_version": CHECKER_VERSION,
        }

    def write_report(
        self,
        report: Mapping[str, Any] | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Create one machine-readable report without replacing an old one."""

        value = dict(report) if report is not None else self.check()
        destination = Path(path) if path is not None else self.run_root / "integrity_report.json"
        encoded = _canonical_json(value) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return destination

    def _reset(self) -> None:
        self.errors = []
        self.warnings = []
        self.counts = {}
        self._records = []
        self._manifests = {}
        self._blob_occurrences = []
        self._verified_blob_paths = set()
        self._secret_hits = set()

    def _load_manifests(self) -> None:
        for name in ("manifest.start.json", "manifest.final.json"):
            path = self.run_root / name
            document = self._read_json_document(path)
            if document is None:
                continue
            self._manifests[name] = document
            self._scan_credential_keys(document, source=path, json_path="$")
            try:
                validate_collector_metadata_keys(document)
            except SchemaValidationError as error:
                self._error(
                    "reserved_evaluation_metadata",
                    str(error),
                    source=path,
                )

            run_id = document.get("run_id")
            if run_id != self.run_root.name:
                self._error(
                    "manifest_run_id_mismatch",
                    "manifest run_id does not match its run directory",
                    source=path,
                )
            if document.get("raw_schema_version") != SCHEMA_VERSION:
                self._error(
                    "manifest_schema_version",
                    f"raw_schema_version must be {SCHEMA_VERSION!r}",
                    source=path,
                )

        start = self._manifests.get("manifest.start.json")
        if start is None:
            return
        missing = sorted(_START_MANIFEST_REQUIRED - start.keys())
        if missing:
            self._error(
                "manifest_start_incomplete",
                "manifest.start.json is missing required collection context",
                source=self.run_root / "manifest.start.json",
                fields=missing,
            )
        commit = start.get("git_commit")
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            self._error(
                "manifest_git_commit",
                "manifest git_commit must be 40 lowercase hexadecimal characters",
                source=self.run_root / "manifest.start.json",
            )
        policy = start.get("collection_policy")
        if isinstance(policy, Mapping):
            if (
                policy.get("label_free") is not True
                or policy.get("prompt_intervention") is not False
            ):
                self._error(
                    "collection_policy_scope",
                    "collection policy must be label-free with prompt intervention disabled",
                    source=self.run_root / "manifest.start.json",
                )

    def _load_event_streams(self) -> None:
        run_path = self.run_root / "run.events.jsonl"
        self._records.extend(self._read_jsonl(run_path, expected_task_run_id=None))

        tasks_root = self.run_root / "tasks"
        if tasks_root.exists() and not tasks_root.is_dir():
            self._error("tasks_not_directory", "tasks path is not a directory", source=tasks_root)
            return
        if not tasks_root.exists():
            self._warning("no_task_streams", "run contains no task stream directory")
            return
        for task_path in sorted(tasks_root.iterdir(), key=lambda path: path.name):
            if task_path.is_symlink() or not task_path.is_dir():
                self._error(
                    "invalid_task_directory",
                    "task entries must be real directories",
                    source=task_path,
                )
                continue
            events_path = task_path / "events.jsonl"
            self._records.extend(self._read_jsonl(events_path, expected_task_run_id=task_path.name))

    def _validate_event_envelopes_and_order(self) -> None:
        event_ids: dict[str, _EventRecord] = {}
        run_event_ids: set[str] = set()
        prior_by_stream: defaultdict[str, set[str]] = defaultdict(set)
        expected_seq: defaultdict[str, int] = defaultdict(lambda: 1)

        for record in self._records:
            event = record.event
            try:
                validate_event_envelope(event)
            except (SchemaValidationError, TypeError, ValueError) as error:
                self._record_error("invalid_event_envelope", str(error), record)

            self._scan_credential_keys(
                event.get("producer"),
                source=record.source,
                json_path=f"$[{record.line}].producer",
                task_run_id=record.task_run_id,
            )
            payload = record.payload
            self._scan_credential_keys(
                payload,
                source=record.source,
                json_path=f"$[{record.line}].payload",
                task_run_id=record.task_run_id,
                opaque_keys=_EVENT_OPAQUE_CREDENTIAL_KEYS.get(record.event_type or "", frozenset()),
            )
            try:
                validate_collector_metadata_keys(
                    payload,
                    opaque_keys=_EVENT_OPAQUE_LABEL_KEYS,
                )
            except SchemaValidationError as error:
                self._record_error("reserved_evaluation_metadata", str(error), record)

            expected_task = record.expected_task_run_id
            if event.get("task_run_id") != expected_task:
                self._record_error(
                    "stream_task_run_id_mismatch",
                    "event task_run_id does not match its physical task stream",
                    record,
                )
            expected_stream = expected_task or self.run_root.name
            if event.get("stream_id") != expected_stream:
                self._record_error(
                    "stream_id_mismatch",
                    "event stream_id does not match its physical stream",
                    record,
                )
            if event.get("run_id") != self.run_root.name:
                self._record_error(
                    "event_run_id_mismatch",
                    "event run_id does not match its run directory",
                    record,
                )

            stream_key = str(record.source)
            seq = record.seq
            if seq != expected_seq[stream_key]:
                self._record_error(
                    "non_contiguous_seq",
                    "stream seq must be unique and contiguous from one",
                    record,
                    expected_seq=expected_seq[stream_key],
                    actual_seq=seq,
                )
            expected_seq[stream_key] += 1

            event_id = record.event_id
            if event_id is not None:
                if event_id in event_ids:
                    self._record_error(
                        "duplicate_event_id",
                        "event_id must be unique across the run",
                        record,
                        event_id=event_id,
                    )
                else:
                    event_ids[event_id] = record

            caused_by = event.get("caused_by_event_id")
            if isinstance(caused_by, str):
                stream_id = str(event.get("stream_id"))
                if caused_by not in prior_by_stream[stream_id] and caused_by not in run_event_ids:
                    self._record_error(
                        "invalid_causal_reference",
                        "causal reference must point to an earlier event in the same task stream",
                        record,
                        referenced_event_id=caused_by,
                    )
            if event_id is not None:
                prior_by_stream[str(event.get("stream_id"))].add(event_id)
                if expected_task is None and record.event_type == "run_started":
                    run_event_ids.add(event_id)

        self.counts["event_count"] = len(self._records)
        self.counts["stream_count"] = len({str(record.source) for record in self._records})
        self.counts["task_stream_count"] = len(
            {record.expected_task_run_id for record in self._records if record.expected_task_run_id}
        )

    def _validate_lifecycle(self) -> None:
        by_source: defaultdict[Path, list[_EventRecord]] = defaultdict(list)
        for record in self._records:
            by_source[record.source].append(record)

        run_source = self.run_root / "run.events.jsonl"
        run_records = by_source.get(run_source, [])
        self._require_lifecycle_pair(
            run_records,
            start_type="run_started",
            end_type="run_ended",
            task_run_id=None,
        )
        for source, records in by_source.items():
            if source == run_source:
                continue
            task_run_id = records[0].expected_task_run_id if records else source.parent.name
            self._require_lifecycle_pair(
                records,
                start_type="task_started",
                end_type="task_ended",
                task_run_id=task_run_id,
            )

    def _require_lifecycle_pair(
        self,
        records: Sequence[_EventRecord],
        *,
        start_type: str,
        end_type: str,
        task_run_id: str | None,
    ) -> None:
        starts = [record for record in records if record.event_type == start_type]
        ends = [record for record in records if record.event_type == end_type]
        source = (
            records[0].source
            if records
            else (
                self.run_root / "run.events.jsonl"
                if task_run_id is None
                else self.run_root / "tasks" / task_run_id / "events.jsonl"
            )
        )
        if len(starts) != 1:
            self._error(
                "lifecycle_start_count",
                f"stream must contain exactly one {start_type}",
                source=source,
                task_run_id=task_run_id,
                count=len(starts),
            )
        if len(ends) != 1:
            self._error(
                "lifecycle_end_count",
                f"stream must contain exactly one {end_type}",
                source=source,
                task_run_id=task_run_id,
                count=len(ends),
            )
        if records and records[0].event_type != start_type:
            self._record_error(
                "lifecycle_start_order",
                f"{start_type} must be the first stream event",
                records[0],
            )
        if records and records[-1].event_type != end_type:
            self._record_error(
                "lifecycle_end_order",
                f"{end_type} must be the final stream event",
                records[-1],
            )

    def _validate_required_event_payloads(self) -> None:
        """Validate collector-defined lifecycle payloads needed for reconstruction."""

        for record in self._records:
            if record.event_type == "task_started":
                self._validate_task_started_payload(record)
            elif record.event_type == "task_ended":
                self._validate_task_ended_payload(record)
            elif record.event_type == "run_ended":
                self._validate_run_ended_payload(record)

    def _validate_task_started_payload(self, record: _EventRecord) -> None:
        payload = record.payload
        if self._require_payload_fields(record, _TASK_STARTED_REQUIRED):
            return

        for field in ("task_name", "suite_family"):
            if not isinstance(payload.get(field), str) or not payload.get(field):
                self._record_error(
                    "invalid_task_started_payload",
                    f"task_started {field} must be a non-empty string",
                    record,
                    field=field,
                )
        for field in ("task_index", "whole_task_attempt_index"):
            if not _is_positive_int(payload.get(field)):
                self._record_error(
                    "invalid_task_started_payload",
                    f"task_started {field} must be a positive integer",
                    record,
                    field=field,
                )
        for field in ("agent", "environment"):
            if not isinstance(payload.get(field), Mapping):
                self._record_error(
                    "invalid_task_started_payload",
                    f"task_started {field} must be an object",
                    record,
                    field=field,
                )

        goal_status = payload.get("task_goal_status")
        goal = payload.get("task_goal")
        if goal_status not in {"resolved", "retrieval_failed"}:
            self._record_error(
                "invalid_task_started_payload",
                "task_goal_status must be resolved or retrieval_failed",
                record,
                field="task_goal_status",
            )
        elif goal_status == "resolved" and not isinstance(goal, str):
            self._record_error(
                "invalid_task_started_payload",
                "a resolved task goal must retain the exact string",
                record,
                field="task_goal",
            )
        elif goal_status == "retrieval_failed" and goal is not None:
            self._record_error(
                "invalid_task_started_payload",
                "a failed task-goal retrieval must retain a null goal",
                record,
                field="task_goal",
            )

    def _validate_task_ended_payload(self, record: _EventRecord) -> None:
        payload = record.payload
        if self._require_payload_fields(record, _TASK_ENDED_REQUIRED):
            return

        runtime_status = payload.get("runtime_status")
        if runtime_status not in _RUNTIME_STATUSES:
            self._record_error(
                "invalid_task_ended_payload",
                "task_ended runtime_status is invalid",
                record,
                field="runtime_status",
            )

        termination = payload.get("termination")
        if not isinstance(termination, Mapping):
            self._record_error(
                "invalid_task_ended_payload",
                "task_ended termination must be an object",
                record,
                field="termination",
            )
        else:
            missing = {"source", "step_index", "exception"} - termination.keys()
            if missing:
                self._record_error(
                    "missing_required_payload_fields",
                    "task_ended termination is missing required fields",
                    record,
                    field="termination",
                    fields=sorted(missing),
                )
            if not isinstance(termination.get("source"), str) or not termination.get("source"):
                self._record_error(
                    "invalid_task_ended_payload",
                    "termination.source must be a non-empty factual string",
                    record,
                    field="termination.source",
                )
            if not _is_nonnegative_int(termination.get("step_index")):
                self._record_error(
                    "invalid_task_ended_payload",
                    "termination.step_index must be a non-negative integer",
                    record,
                    field="termination.step_index",
                )
            if "exception" in termination and not _is_optional_mapping(
                termination.get("exception")
            ):
                self._record_error(
                    "invalid_task_ended_payload",
                    "termination.exception must be an object or null",
                    record,
                    field="termination.exception",
                )

        evaluation = payload.get("environment_evaluation")
        if not isinstance(evaluation, Mapping):
            self._record_error(
                "invalid_task_ended_payload",
                "environment_evaluation must be an object",
                record,
                field="environment_evaluation",
            )
        else:
            missing = {"score", "reason", "exception"} - evaluation.keys()
            if missing:
                self._record_error(
                    "missing_required_payload_fields",
                    "environment_evaluation is missing required fields",
                    record,
                    field="environment_evaluation",
                    fields=sorted(missing),
                )
            score = evaluation.get("score")
            if score is not None and not _is_number(score):
                self._record_error(
                    "invalid_task_ended_payload",
                    "environment_evaluation.score must be numeric or null",
                    record,
                    field="environment_evaluation.score",
                )
            if runtime_status == "completed" and not _is_number(score):
                self._record_error(
                    "invalid_task_ended_payload",
                    "a completed task must retain its numeric environment score",
                    record,
                    field="environment_evaluation.score",
                )
            reason = evaluation.get("reason")
            if reason is not None and not isinstance(reason, str):
                self._record_error(
                    "invalid_task_ended_payload",
                    "environment_evaluation.reason must be a string or null",
                    record,
                    field="environment_evaluation.reason",
                )
            if "exception" in evaluation and not _is_optional_mapping(evaluation.get("exception")):
                self._record_error(
                    "invalid_task_ended_payload",
                    "environment_evaluation.exception must be an object or null",
                    record,
                    field="environment_evaluation.exception",
                )

        teardown = payload.get("teardown")
        if not isinstance(teardown, Mapping):
            self._record_error(
                "invalid_task_ended_payload",
                "teardown must be an object",
                record,
                field="teardown",
            )
        else:
            missing = {"returned", "result_snapshot_blob", "exception"} - teardown.keys()
            if missing:
                self._record_error(
                    "missing_required_payload_fields",
                    "teardown is missing required fields",
                    record,
                    field="teardown",
                    fields=sorted(missing),
                )
            if not isinstance(teardown.get("returned"), bool):
                self._record_error(
                    "invalid_task_ended_payload",
                    "teardown.returned must be a boolean",
                    record,
                    field="teardown.returned",
                )
            result_blob = teardown.get("result_snapshot_blob")
            if result_blob is not None and not _is_blob_reference(result_blob):
                self._record_error(
                    "invalid_task_ended_payload",
                    "teardown.result_snapshot_blob must be a BlobRef or null",
                    record,
                    field="teardown.result_snapshot_blob",
                )
            if "exception" in teardown and not _is_optional_mapping(teardown.get("exception")):
                self._record_error(
                    "invalid_task_ended_payload",
                    "teardown.exception must be an object or null",
                    record,
                    field="teardown.exception",
                )

        token_usage = payload.get("token_usage")
        if not isinstance(token_usage, Mapping):
            self._record_error(
                "invalid_task_ended_payload",
                "token_usage must be an object",
                record,
                field="token_usage",
            )
        else:
            for key, value in token_usage.items():
                if not isinstance(key, str) or not _is_nonnegative_int(value):
                    self._record_error(
                        "invalid_task_ended_payload",
                        "token_usage fields must be non-negative integer counters",
                        record,
                        field=f"token_usage.{key}",
                    )

        if not isinstance(payload.get("capture_complete"), bool):
            self._record_error(
                "invalid_task_ended_payload",
                "task_ended capture_complete must be a boolean",
                record,
                field="capture_complete",
            )
        self._validate_string_list(record, "missing_artifacts", "invalid_task_ended_payload")
        self._validate_string_list(
            record,
            "collector_error_event_ids",
            "invalid_task_ended_payload",
            unique=True,
        )

    def _validate_run_ended_payload(self, record: _EventRecord) -> None:
        payload = record.payload
        if self._require_payload_fields(record, _RUN_ENDED_REQUIRED):
            return
        if payload.get("runtime_status") not in _RUNTIME_STATUSES:
            self._record_error(
                "invalid_run_ended_payload",
                "run_ended runtime_status is invalid",
                record,
                field="runtime_status",
            )
        self._validate_string_list(
            record,
            "task_run_ids",
            "invalid_run_ended_payload",
            unique=True,
        )
        self._validate_string_list(
            record,
            "collector_error_event_ids",
            "invalid_run_ended_payload",
            unique=True,
        )
        counts = payload.get("task_counts")
        if not isinstance(counts, Mapping) or set(counts) != {"started", "completed", "crashed"}:
            self._record_error(
                "invalid_run_ended_payload",
                "run_ended task_counts must contain started/completed/crashed",
                record,
                field="task_counts",
            )
        elif any(not _is_nonnegative_int(value) for value in counts.values()):
            self._record_error(
                "invalid_run_ended_payload",
                "run_ended task_counts values must be non-negative integers",
                record,
                field="task_counts",
            )
        if not isinstance(payload.get("capture_complete"), bool):
            self._record_error(
                "invalid_run_ended_payload",
                "run_ended capture_complete must be a boolean",
                record,
                field="capture_complete",
            )
        if payload.get("manifest_final_path") != "manifest.final.json":
            self._record_error(
                "invalid_run_ended_payload",
                "run_ended must reference manifest.final.json",
                record,
                field="manifest_final_path",
            )

    def _require_payload_fields(
        self,
        record: _EventRecord,
        required: frozenset[str],
    ) -> bool:
        missing = sorted(required - record.payload.keys())
        if not missing:
            return False
        self._record_error(
            "missing_required_payload_fields",
            f"{record.event_type} is missing required fields",
            record,
            fields=missing,
        )
        return True

    def _validate_string_list(
        self,
        record: _EventRecord,
        field: str,
        code: str,
        *,
        unique: bool = False,
    ) -> None:
        value = record.payload.get(field)
        valid = (
            isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            and (not unique or len(value) == len(set(value)))
        )
        if not valid:
            self._record_error(
                code,
                f"{field} must be an ordered list of non-empty strings"
                + (" without duplicates" if unique else ""),
                record,
                field=field,
            )

    def _validate_manifest_summaries(self) -> None:
        final = self._manifests.get("manifest.final.json")
        if final is None:
            return
        source = self.run_root / "manifest.final.json"
        missing = sorted(_FINAL_MANIFEST_REQUIRED - final.keys())
        if missing:
            self._error(
                "manifest_final_incomplete",
                "manifest.final.json is missing required finalization fields",
                source=source,
                fields=missing,
            )

        if final.get("runtime_status") not in _RUNTIME_STATUSES:
            self._error(
                "manifest_final_runtime_status",
                "final manifest runtime_status is invalid",
                source=source,
            )
        if not isinstance(final.get("ended_at_utc"), str) or not final.get("ended_at_utc"):
            self._error(
                "manifest_final_end_time",
                "final manifest ended_at_utc must be a non-empty timestamp",
                source=source,
            )
        if not isinstance(final.get("capture_complete"), bool):
            self._error(
                "manifest_final_capture_status",
                "final manifest capture_complete must be a boolean",
                source=source,
            )
        for field in ("missing_artifacts", "collector_error_event_ids"):
            if not _is_string_list(final.get(field), unique=True):
                self._error(
                    "manifest_final_list_shape",
                    f"final manifest {field} must be an ordered unique string list",
                    source=source,
                    field=field,
                )

        self._validate_manifest_file_summary(
            final.get("manifest_start"),
            self.run_root / "manifest.start.json",
            summary_name="manifest_start",
        )
        self._validate_manifest_file_summary(
            final.get("run_stream"),
            self.run_root / "run.events.jsonl",
            summary_name="run_stream",
        )

        raw_summaries = final.get("task_streams")
        if not isinstance(raw_summaries, list):
            self._error(
                "manifest_task_streams_shape",
                "final manifest task_streams must be an ordered list",
                source=source,
            )
            summaries: list[Mapping[str, Any]] = []
        else:
            summaries = []
            for index, value in enumerate(raw_summaries):
                if not isinstance(value, Mapping):
                    self._error(
                        "manifest_task_summary_shape",
                        "each task stream summary must be an object",
                        source=source,
                        task_summary_index=index,
                    )
                    continue
                summaries.append(value)

        task_ends = {
            record.task_run_id: record
            for record in self._records
            if record.event_type == "task_ended" and record.task_run_id is not None
        }
        summary_ids: list[str] = []
        for index, summary in enumerate(summaries):
            task_run_id = summary.get("task_run_id")
            if not isinstance(task_run_id, str) or not task_run_id:
                self._error(
                    "manifest_task_summary_shape",
                    "task stream summary requires a task_run_id",
                    source=source,
                    task_summary_index=index,
                )
                continue
            if task_run_id in summary_ids:
                self._error(
                    "manifest_duplicate_task_summary",
                    "task_run_id occurs more than once in final task_streams",
                    source=source,
                    task_run_id=task_run_id,
                )
            summary_ids.append(task_run_id)
            self._validate_task_manifest_summary(
                summary,
                index=index,
                task_end=task_ends.get(task_run_id),
            )

        actual_task_ids = self._actual_task_stream_ids()
        if len(summary_ids) != len(set(summary_ids)) or set(summary_ids) != set(actual_task_ids):
            self._error(
                "manifest_task_stream_set_mismatch",
                "final task_streams must enumerate every physical task stream exactly once",
                source=source,
                expected=sorted(actual_task_ids),
                actual=summary_ids,
            )

        self._validate_manifest_blob_counts(final)
        self._validate_run_end_against_manifest(final, summaries, summary_ids)
        self._validate_manifest_collector_errors(final, summary_ids)

        summary_capture_values = [summary.get("capture_complete") for summary in summaries]
        final_missing = final.get("missing_artifacts")
        final_error_ids = final.get("collector_error_event_ids")
        if (
            all(isinstance(value, bool) for value in summary_capture_values)
            and _is_string_list(final_missing, unique=True)
            and _is_string_list(final_error_ids, unique=True)
            and isinstance(final.get("capture_complete"), bool)
        ):
            expected_capture = (
                all(summary_capture_values) and not final_missing and not final_error_ids
            )
            if final.get("capture_complete") != expected_capture:
                self._error(
                    "manifest_capture_status_mismatch",
                    "final capture_complete is inconsistent with task/run missing evidence",
                    source=source,
                    expected=expected_capture,
                    actual=final.get("capture_complete"),
                )

    def _validate_task_manifest_summary(
        self,
        summary: Mapping[str, Any],
        *,
        index: int,
        task_end: _EventRecord | None,
    ) -> None:
        source = self.run_root / "manifest.final.json"
        task_run_id = str(summary.get("task_run_id"))
        missing = sorted(_TASK_SUMMARY_REQUIRED - summary.keys())
        if missing:
            self._error(
                "manifest_task_summary_incomplete",
                "task stream summary is missing required fields",
                source=source,
                task_run_id=task_run_id,
                task_summary_index=index,
                fields=missing,
            )
        expected_relative = f"tasks/{task_run_id}/events.jsonl"
        if summary.get("relative_path") != expected_relative:
            self._error(
                "manifest_task_path_mismatch",
                "task stream relative_path does not match task_run_id",
                source=source,
                task_run_id=task_run_id,
                expected=expected_relative,
                actual=summary.get("relative_path"),
            )
        self._validate_manifest_file_summary(
            summary,
            self.run_root / "tasks" / task_run_id / "events.jsonl",
            summary_name=f"task_stream:{task_run_id}",
            task_run_id=task_run_id,
        )
        if summary.get("runtime_status") not in _RUNTIME_STATUSES:
            self._error(
                "manifest_task_status_shape",
                "task summary runtime_status is invalid",
                source=source,
                task_run_id=task_run_id,
            )
        for field in ("retry_planned", "capture_complete"):
            if not isinstance(summary.get(field), bool):
                self._error(
                    "manifest_task_status_shape",
                    f"task summary {field} must be a boolean",
                    source=source,
                    task_run_id=task_run_id,
                    field=field,
                )
        for field in ("missing_artifacts", "collector_error_event_ids"):
            if not _is_string_list(summary.get(field), unique=True):
                self._error(
                    "manifest_task_status_shape",
                    f"task summary {field} must be an ordered unique string list",
                    source=source,
                    task_run_id=task_run_id,
                    field=field,
                )

        if task_end is None:
            return
        for field in (
            "runtime_status",
            "capture_complete",
            "missing_artifacts",
            "collector_error_event_ids",
        ):
            if summary.get(field) != task_end.payload.get(field):
                self._error(
                    "manifest_task_terminal_mismatch",
                    f"task summary {field} does not match task_ended",
                    source=source,
                    task_run_id=task_run_id,
                    field=field,
                )

        actual_error_ids = [
            record.event_id
            for record in self._records
            if record.task_run_id == task_run_id and record.event_type == "collector_error"
        ]
        if task_end.payload.get("collector_error_event_ids") != actual_error_ids:
            self._record_error(
                "task_collector_error_references",
                "task_ended must reference exactly its collector_error events in order",
                task_end,
                expected=actual_error_ids,
                actual=task_end.payload.get("collector_error_event_ids"),
            )

    def _validate_manifest_file_summary(
        self,
        summary: Any,
        path: Path,
        *,
        summary_name: str,
        task_run_id: str | None = None,
    ) -> None:
        source = self.run_root / "manifest.final.json"
        if not isinstance(summary, Mapping):
            self._error(
                "manifest_file_summary_shape",
                f"{summary_name} summary must be an object",
                source=source,
                task_run_id=task_run_id,
            )
            return
        digest = summary.get("sha256")
        byte_count = summary.get("byte_count")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            self._error(
                "manifest_file_summary_shape",
                f"{summary_name} sha256 is invalid",
                source=source,
                task_run_id=task_run_id,
            )
            return
        if not _is_nonnegative_int(byte_count):
            self._error(
                "manifest_file_summary_shape",
                f"{summary_name} byte_count is invalid",
                source=source,
                task_run_id=task_run_id,
            )
            return
        if path.is_symlink() or not path.is_file():
            self._error(
                "manifest_file_missing",
                f"{summary_name} summarized file is missing or not regular",
                source=path,
                task_run_id=task_run_id,
            )
            return
        actual = _file_summary(path)
        if digest != actual["sha256"] or byte_count != actual["byte_count"]:
            self._error(
                "manifest_file_summary_mismatch",
                f"{summary_name} checksum or byte count does not match stored bytes",
                source=source,
                task_run_id=task_run_id,
                summary=summary_name,
            )

    def _validate_manifest_blob_counts(self, final: Mapping[str, Any]) -> None:
        source = self.run_root / "manifest.final.json"
        root = self.run_root / "blobs" / "sha256"
        paths = (
            [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
            if root.is_dir()
            else []
        )
        expected = {
            "blob_count": len(paths),
            "blob_byte_count": sum(p.stat().st_size for p in paths),
        }
        for field, value in expected.items():
            if not _is_nonnegative_int(final.get(field)) or final.get(field) != value:
                self._error(
                    "manifest_blob_summary_mismatch",
                    f"final manifest {field} does not match content-addressed storage",
                    source=source,
                    field=field,
                    expected=value,
                    actual=final.get(field),
                )

    def _validate_run_end_against_manifest(
        self,
        final: Mapping[str, Any],
        summaries: Sequence[Mapping[str, Any]],
        summary_ids: Sequence[str],
    ) -> None:
        run_ends = [record for record in self._records if record.event_type == "run_ended"]
        if len(run_ends) != 1:
            return
        run_end = run_ends[0]
        payload = run_end.payload
        if payload.get("task_run_ids") != list(summary_ids):
            self._record_error(
                "run_task_list_mismatch",
                "run_ended task_run_ids must match final task_streams in order",
                run_end,
                expected=list(summary_ids),
                actual=payload.get("task_run_ids"),
            )
        completed = sum(summary.get("runtime_status") == "completed" for summary in summaries)
        expected_counts = {
            "started": len(summaries),
            "completed": completed,
            "crashed": len(summaries) - completed,
        }
        if payload.get("task_counts") != expected_counts:
            self._record_error(
                "run_task_counts_mismatch",
                "run_ended task_counts do not match finalized task summaries",
                run_end,
                expected=expected_counts,
                actual=payload.get("task_counts"),
            )
        for field in ("runtime_status", "capture_complete", "collector_error_event_ids"):
            if payload.get(field) != final.get(field):
                self._record_error(
                    "run_final_manifest_mismatch",
                    f"run_ended {field} does not match manifest.final.json",
                    run_end,
                    field=field,
                )

    def _validate_manifest_collector_errors(
        self,
        final: Mapping[str, Any],
        summary_ids: Sequence[str],
    ) -> None:
        expected = [
            record.event_id
            for record in self._records
            if record.event_type == "collector_error" and record.task_run_id is None
        ]
        for task_run_id in summary_ids:
            expected.extend(
                record.event_id
                for record in self._records
                if record.event_type == "collector_error" and record.task_run_id == task_run_id
            )
        expected_ids = [event_id for event_id in expected if isinstance(event_id, str)]
        actual = final.get("collector_error_event_ids")
        if _is_string_list(actual, unique=True) and actual != expected_ids:
            self._error(
                "manifest_collector_error_references",
                "final manifest must reference every collector_error event in finalization order",
                source=self.run_root / "manifest.final.json",
                expected=expected_ids,
                actual=actual,
            )

    def _actual_task_stream_ids(self) -> list[str]:
        root = self.run_root / "tasks"
        if not root.is_dir():
            return []
        return sorted(
            path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
        )

    def _validate_model_attempts(self) -> None:
        requests: dict[str, _EventRecord] = {}
        terminals: defaultdict[str, list[_EventRecord]] = defaultdict(list)
        chunks: defaultdict[str, list[_EventRecord]] = defaultdict(list)

        for record in self._records:
            if record.event_type in {
                "model_request",
                "model_stream_chunk",
                "model_response",
                "model_attempt_failed",
            }:
                self._validate_required_model_payload(record)
            request_id = record.payload.get("request_id")
            if not isinstance(request_id, str):
                if record.event_type in {
                    "model_request",
                    "model_stream_chunk",
                    "model_response",
                    "model_attempt_failed",
                }:
                    self._record_error(
                        "missing_request_id",
                        "model attempt events require a string request_id",
                        record,
                    )
                continue
            if record.event_type == "model_request":
                self._validate_model_request_step(record)
                if request_id in requests:
                    self._record_error(
                        "duplicate_request_id",
                        "request_id must identify one SDK invocation",
                        record,
                        request_id=request_id,
                    )
                else:
                    requests[request_id] = record
            elif record.event_type in {"model_response", "model_attempt_failed"}:
                terminals[request_id].append(record)
            elif record.event_type == "model_stream_chunk":
                chunks[request_id].append(record)

        for request_id, request in requests.items():
            attempt_terminals = terminals.get(request_id, [])
            if len(attempt_terminals) != 1:
                self._record_error(
                    "model_terminal_count",
                    "every model_request requires exactly one response or failure terminal",
                    request,
                    request_id=request_id,
                    count=len(attempt_terminals),
                )
            for terminal in attempt_terminals:
                self._validate_attempt_correlation(request, terminal)
            self._validate_request_chunks(
                request,
                attempt_terminals[0] if len(attempt_terminals) == 1 else None,
                chunks.get(request_id, []),
            )

        for request_id, records in terminals.items():
            if request_id not in requests:
                for record in records:
                    self._record_error(
                        "terminal_without_request",
                        "model terminal references no earlier model_request",
                        record,
                        request_id=request_id,
                    )
        for request_id, records in chunks.items():
            if request_id not in requests:
                for record in records:
                    self._record_error(
                        "chunk_without_request",
                        "stream chunk references no model_request",
                        record,
                        request_id=request_id,
                    )

        self._validate_retry_indices(list(requests.values()))
        self.counts["model_request_count"] = len(requests)
        self.counts["model_terminal_count"] = sum(len(values) for values in terminals.values())
        self.counts["model_stream_chunk_count"] = sum(len(values) for values in chunks.values())

    def _validate_required_model_payload(self, record: _EventRecord) -> None:
        payload = record.payload
        for field in ("step_id", "model_call_id", "retry_group_id", "request_id"):
            if not isinstance(payload.get(field), str):
                self._record_error(
                    "missing_model_correlation",
                    f"model event requires a string {field}",
                    record,
                    field=field,
                )
        for field in ("adapter_attempt_index", "attempt_index"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                self._record_error(
                    "invalid_model_attempt_index",
                    f"model event requires a positive integer {field}",
                    record,
                    field=field,
                )

        if record.event_type == "model_request":
            snapshot_reference = payload.get("sdk_arguments_snapshot_blob")
            if not _is_blob_reference(snapshot_reference):
                self._record_error(
                    "missing_required_artifact",
                    "model_request requires an authoritative SDK-arguments snapshot blob",
                    record,
                    field="sdk_arguments_snapshot_blob",
                )
            else:
                self._validate_request_view_against_artifact(record, snapshot_reference)
            request_images = payload.get("request_images")
            if not isinstance(request_images, Sequence) or isinstance(request_images, str):
                self._record_error(
                    "invalid_request_images",
                    "model_request request_images must be an ordered list",
                    record,
                )
            else:
                for index, image in enumerate(request_images):
                    if not isinstance(image, Mapping):
                        self._record_error(
                            "invalid_request_image",
                            "request image metadata must be an object",
                            record,
                            image_index=index,
                        )
                    elif image.get("capture_status") == "captured" and (
                        not _is_blob_reference(image.get("original_text_blob"))
                        or not _is_blob_reference(image.get("content_blob"))
                    ):
                        self._record_error(
                            "missing_required_artifact",
                            "captured request image requires original-text and content blobs",
                            record,
                            image_index=index,
                        )
        elif record.event_type == "model_stream_chunk":
            if not _is_blob_reference(payload.get("raw_chunk_snapshot_blob")):
                self._record_error(
                    "missing_required_artifact",
                    "model_stream_chunk requires its raw chunk snapshot blob",
                    record,
                    field="raw_chunk_snapshot_blob",
                )
        elif record.event_type == "model_response":
            raw_response = payload.get("raw_response")
            if not isinstance(raw_response, Mapping):
                self._record_error(
                    "invalid_raw_response",
                    "model_response requires raw_response metadata",
                    record,
                )
            elif payload.get("response_mode") == "non_stream" and not _is_blob_reference(
                raw_response.get("snapshot_blob")
            ):
                self._record_error(
                    "missing_required_artifact",
                    "non-stream model_response requires its raw response snapshot blob",
                    record,
                    field="raw_response.snapshot_blob",
                )

    def _validate_model_request_step(self, request: _EventRecord) -> None:
        step_id = request.payload.get("step_id")
        matching_steps = [
            record
            for record in self._records
            if record.event_type == "step_started" and record.payload.get("step_id") == step_id
        ]
        if len(matching_steps) != 1 or not self._is_earlier_same_task(matching_steps[0], request):
            self._record_error(
                "model_request_step_reference",
                "model_request must link one earlier step_started in the same task stream",
                request,
                step_id=step_id,
            )

    def _validate_request_view_against_artifact(
        self,
        request: _EventRecord,
        reference: Mapping[str, Any],
    ) -> None:
        try:
            graph = self.artifact_serializer.load_graph(dict(reference))
            expected_view = self._artifact_request_view(graph)
        except (
            BlobIntegrityError,
            KeyError,
            OSError,
            SerializationError,
            TypeError,
            ValueError,
        ) as error:
            self._record_error(
                "request_view_artifact_unreadable",
                f"could not reconstruct inspectable request view: {error}",
                request,
            )
            return
        if _canonical_json_value(request.payload.get("request_view")) != _canonical_json_value(
            expected_view
        ):
            self._record_error(
                "request_view_artifact_mismatch",
                "model_request request_view is inconsistent with its authoritative artifact graph",
                request,
            )

    def _artifact_request_view(self, graph: Mapping[str, Any]) -> Any:
        version = graph.get("artifact_graph_version")
        root = graph.get("root")
        if not isinstance(version, str) or not isinstance(root, Mapping):
            raise SerializationError("artifact graph has no typed root")
        return self._artifact_node_request_view(root, graph_version=version)

    def _artifact_node_request_view(
        self,
        node: Mapping[str, Any],
        *,
        graph_version: str,
    ) -> Any:
        node_type = node.get("node_type")
        if node_type == "null":
            return None
        if node_type in {"bool", "int", "float", "string"}:
            return node["value"]
        if node_type == "data_url":
            return {
                "$externalized_data_url": {
                    "original_text_blob": node["original_text_blob"],
                    "content_blob": node["content_blob"],
                    "media_type": node["media_type"],
                    "base64_alphabet": node["base64_alphabet"],
                    "content_path": node["content_path"],
                }
            }
        if node_type == "binary":
            return {
                "$externalized_blob": {
                    "blob": node["blob"],
                    "original_type": node["original_type"],
                }
            }
        if node_type == "sequence":
            items = [
                self._artifact_node_request_view(item, graph_version=graph_version)
                for item in _mapping_sequence(node.get("items"), "artifact sequence items")
            ]
            if node.get("sequence_type") == "tuple":
                return {"$typed_value": {"kind": "tuple", "items": items}}
            if node.get("sequence_type") != "list":
                raise SerializationError("artifact sequence has an invalid sequence_type")
            return items
        if node_type == "mapping":
            items = _mapping_sequence(node.get("items"), "artifact mapping items")
            string_keys = all(
                isinstance(item.get("key"), Mapping) and item["key"].get("node_type") == "string"
                for item in items
            )
            if string_keys:
                return {
                    item["key"]["value"]: self._artifact_node_request_view(
                        _mapping_node(item.get("value"), "artifact mapping value"),
                        graph_version=graph_version,
                    )
                    for item in items
                }
            return {
                "$typed_mapping": [
                    {
                        "key": self._artifact_nonstring_item_view(
                            _mapping_node(item.get("key"), "artifact mapping key"),
                            graph_version=graph_version,
                        ),
                        "value": self._artifact_nonstring_item_view(
                            _mapping_node(item.get("value"), "artifact mapping value"),
                            graph_version=graph_version,
                        ),
                    }
                    for item in items
                ]
            }
        if node_type == "serialized_object":
            return self._artifact_node_request_view(
                _mapping_node(node.get("value"), "serialized object value"),
                graph_version=graph_version,
            )
        if node_type in {"typed_scalar", "repr_fallback"}:
            kind = node.get("kind", "repr_fallback")
            return {
                "$typed_value": {
                    "kind": kind,
                    "class": node["class"],
                    "value": node["value"],
                }
            }
        raise SerializationError(f"unknown artifact node_type: {node_type!r}")

    def _artifact_nonstring_item_view(
        self,
        node: Mapping[str, Any],
        *,
        graph_version: str,
    ) -> Any:
        if node.get("node_type") == "data_url":
            return self._artifact_node_request_view(node, graph_version=graph_version)
        return self.artifact_serializer.rehydrate(
            {"artifact_graph_version": graph_version, "root": dict(node)}
        )

    def _validate_attempt_correlation(self, request: _EventRecord, terminal: _EventRecord) -> None:
        if terminal.seq is not None and request.seq is not None and terminal.seq <= request.seq:
            self._record_error(
                "model_terminal_order",
                "model terminal must follow its request in the same stream",
                terminal,
            )
        if terminal.expected_task_run_id != request.expected_task_run_id:
            self._record_error(
                "model_terminal_stream",
                "model request and terminal must be in the same task stream",
                terminal,
            )
        for field in (
            "step_id",
            "model_call_id",
            "retry_group_id",
            "adapter_attempt_index",
            "request_id",
            "attempt_index",
        ):
            if terminal.payload.get(field) != request.payload.get(field):
                self._record_error(
                    "model_terminal_correlation",
                    f"model terminal {field} does not match its request",
                    terminal,
                    field=field,
                )

    def _validate_request_chunks(
        self,
        request: _EventRecord,
        terminal: _EventRecord | None,
        records: Sequence[_EventRecord],
    ) -> None:
        ordered = sorted(records, key=lambda record: record.seq or -1)
        actual_indices = [record.payload.get("chunk_index") for record in ordered]
        expected_indices = list(range(len(ordered)))
        if actual_indices != expected_indices:
            self._record_error(
                "non_contiguous_chunk_index",
                "chunk_index must be contiguous from zero for each request",
                request,
                expected=expected_indices,
                actual=actual_indices,
            )
        for chunk in ordered:
            if chunk.expected_task_run_id != request.expected_task_run_id:
                self._record_error(
                    "chunk_stream_mismatch",
                    "model chunk must share the request task stream",
                    chunk,
                )
            if chunk.seq is not None and request.seq is not None and chunk.seq <= request.seq:
                self._record_error(
                    "chunk_order",
                    "model chunk must follow its request",
                    chunk,
                )
            self._validate_attempt_correlation_fields(request, chunk)

        if terminal is None:
            return
        expected_ids = [record.event_id for record in ordered]
        if terminal.event_type == "model_response":
            raw_response = terminal.payload.get("raw_response")
            if isinstance(raw_response, Mapping):
                if raw_response.get("chunk_event_ids") != expected_ids:
                    self._record_error(
                        "response_chunk_references",
                        "model_response must reference exactly the recorded chunks in order",
                        terminal,
                    )
                if raw_response.get("chunk_count") != len(ordered):
                    self._record_error(
                        "response_chunk_count",
                        "model_response chunk_count does not match recorded chunks",
                        terminal,
                    )
        elif terminal.payload.get("partial_chunk_event_ids") != expected_ids:
            self._record_error(
                "failure_chunk_references",
                "model_attempt_failed must reference exactly the recorded partial chunks",
                terminal,
            )

    def _validate_attempt_correlation_fields(
        self, request: _EventRecord, related: _EventRecord
    ) -> None:
        for field in (
            "step_id",
            "model_call_id",
            "retry_group_id",
            "adapter_attempt_index",
            "request_id",
            "attempt_index",
        ):
            if related.payload.get(field) != request.payload.get(field):
                self._record_error(
                    "model_attempt_correlation",
                    f"model attempt event {field} does not match its request",
                    related,
                    field=field,
                )

    def _validate_retry_indices(self, requests: Sequence[_EventRecord]) -> None:
        by_model_call: defaultdict[tuple[str | None, Any], list[_EventRecord]] = defaultdict(list)
        by_retry_group: defaultdict[tuple[str | None, Any], list[_EventRecord]] = defaultdict(list)
        for request in requests:
            by_model_call[(request.task_run_id, request.payload.get("model_call_id"))].append(
                request
            )
            by_retry_group[(request.task_run_id, request.payload.get("retry_group_id"))].append(
                request
            )

        for records in by_model_call.values():
            ordered = sorted(records, key=lambda record: record.seq or -1)
            actual = [record.payload.get("attempt_index") for record in ordered]
            if actual != list(range(1, len(ordered) + 1)):
                self._record_error(
                    "invalid_attempt_indices",
                    "attempt_index must increase contiguously within model_call_id",
                    ordered[0],
                    actual=actual,
                )

        for records in by_retry_group.values():
            ordered = sorted(records, key=lambda record: record.seq or -1)
            attempts: list[tuple[Any, Any]] = []
            for record in ordered:
                pair = (
                    record.payload.get("adapter_attempt_index"),
                    record.payload.get("model_call_id"),
                )
                if pair not in attempts:
                    attempts.append(pair)
            indices = [pair[0] for pair in attempts]
            if indices != list(range(1, len(indices) + 1)):
                self._record_error(
                    "invalid_adapter_attempt_indices",
                    "adapter_attempt_index must increase contiguously within retry_group_id",
                    ordered[0],
                    actual=indices,
                )

    def _validate_transitions(self) -> None:
        steps = self._unique_payload_index("step_started", "step_id")
        decisions = self._unique_payload_index("agent_decision", "decision_id")
        executions = self._unique_payload_index("action_execution_started", "execution_id")
        execution_terminals: defaultdict[str, list[_EventRecord]] = defaultdict(list)
        decision_terminals: defaultdict[str, list[_EventRecord]] = defaultdict(list)

        for record in self._records:
            if record.event_type == "agent_decision":
                self._validate_decision(record, steps)
            elif record.event_type == "action_execution_started":
                self._validate_execution(record, decisions)
            elif record.event_type in {"transition_completed", "transition_failed"}:
                execution_id = record.payload.get("execution_id")
                if isinstance(execution_id, str):
                    execution_terminals[execution_id].append(record)
                decision_id = record.payload.get("decision_id")
                if isinstance(decision_id, str):
                    decision_terminals[decision_id].append(record)
                self._validate_executed_transition(record, steps, decisions, executions)
            elif record.event_type == "transition_not_executed":
                decision_id = record.payload.get("decision_id")
                if isinstance(decision_id, str):
                    decision_terminals[decision_id].append(record)
                self._validate_not_executed_transition(record, steps, decisions)

        for execution_id, execution in executions.items():
            terminal_records = execution_terminals.get(execution_id, [])
            if len(terminal_records) != 1:
                self._record_error(
                    "execution_terminal_count",
                    "every action execution requires exactly one completed or failed transition",
                    execution,
                    execution_id=execution_id,
                    count=len(terminal_records),
                )
        for decision_id, decision in decisions.items():
            terminal_records = decision_terminals.get(decision_id, [])
            if len(terminal_records) != 1:
                self._record_error(
                    "decision_terminal_count",
                    "every agent decision requires exactly one transition outcome",
                    decision,
                    decision_id=decision_id,
                    count=len(terminal_records),
                )

        self._validate_max_step_transitions(decisions, executions, execution_terminals)
        self.counts["step_count"] = len(steps)
        self.counts["decision_count"] = len(decisions)
        self.counts["action_execution_count"] = len(executions)

    def _unique_payload_index(self, event_type: str, field: str) -> dict[str, _EventRecord]:
        result: dict[str, _EventRecord] = {}
        for record in self._records:
            if record.event_type != event_type:
                continue
            value = record.payload.get(field)
            if not isinstance(value, str):
                self._record_error(
                    "missing_correlation_id",
                    f"{event_type} requires a string {field}",
                    record,
                    field=field,
                )
            elif value in result:
                self._record_error(
                    "duplicate_correlation_id",
                    f"{field} must be unique within the run",
                    record,
                    field=field,
                )
            else:
                result[value] = record
        return result

    def _validate_decision(self, decision: _EventRecord, steps: Mapping[str, _EventRecord]) -> None:
        step = steps.get(str(decision.payload.get("step_id")))
        if step is None or not self._is_earlier_same_task(step, decision):
            self._record_error(
                "decision_step_reference",
                "agent_decision must link an earlier step_started in the same task",
                decision,
            )

        source_ids = decision.payload.get("source_model_call_ids")
        if not (
            isinstance(source_ids, list)
            and all(isinstance(item, str) and item for item in source_ids)
            and len(source_ids) == len(set(source_ids))
        ):
            self._record_error(
                "invalid_decision_model_sources",
                "agent_decision source_model_call_ids must be an ordered unique string list",
                decision,
            )
            return

        expected_ids: list[str] = []
        for request in self._records:
            if (
                request.event_type != "model_request"
                or request.task_run_id != decision.task_run_id
                or request.payload.get("step_id") != decision.payload.get("step_id")
                or request.seq is None
                or decision.seq is None
                or request.seq >= decision.seq
            ):
                continue
            model_call_id = request.payload.get("model_call_id")
            if isinstance(model_call_id, str) and model_call_id not in expected_ids:
                expected_ids.append(model_call_id)
        if source_ids != expected_ids:
            self._record_error(
                "decision_model_sources_mismatch",
                "agent_decision must reference exactly the earlier model calls for its step",
                decision,
                expected=expected_ids,
                actual=source_ids,
            )

    def _validate_execution(
        self, execution: _EventRecord, decisions: Mapping[str, _EventRecord]
    ) -> None:
        decision = decisions.get(str(execution.payload.get("decision_id")))
        if decision is None or not self._is_earlier_same_task(decision, execution):
            self._record_error(
                "execution_decision_reference",
                "action_execution_started must link an earlier decision in the same task",
                execution,
            )
        elif execution.payload.get("step_id") != decision.payload.get("step_id"):
            self._record_error(
                "execution_step_mismatch",
                "action execution step_id must match its decision",
                execution,
            )

    def _validate_executed_transition(
        self,
        transition: _EventRecord,
        steps: Mapping[str, _EventRecord],
        decisions: Mapping[str, _EventRecord],
        executions: Mapping[str, _EventRecord],
    ) -> None:
        payload = transition.payload
        step = steps.get(str(payload.get("step_id")))
        decision = decisions.get(str(payload.get("decision_id")))
        execution = executions.get(str(payload.get("execution_id")))
        if step is None or payload.get("pre_observation_event_id") != step.event_id:
            self._record_error(
                "transition_pre_observation_reference",
                "transition must link the exact step_started event",
                transition,
            )
        if decision is None or decision.payload.get("step_id") != payload.get("step_id"):
            self._record_error(
                "transition_decision_reference",
                "transition must link the exact decision for its step",
                transition,
            )
        if (
            execution is None
            or payload.get("action_execution_event_id") != execution.event_id
            or execution.payload.get("decision_id") != payload.get("decision_id")
        ):
            self._record_error(
                "transition_execution_reference",
                "transition must link the exact action execution event",
                transition,
            )
        for related in (step, decision, execution):
            if related is not None and not self._is_earlier_same_task(related, transition):
                self._record_error(
                    "transition_reference_order",
                    "transition references must be earlier in the same task stream",
                    transition,
                )
                break
        if (
            transition.event_type == "transition_completed"
            and payload.get("post_observation") is None
        ):
            self._record_error(
                "completed_transition_missing_post_observation",
                "transition_completed must retain the returned post-observation",
                transition,
            )

    def _validate_not_executed_transition(
        self,
        transition: _EventRecord,
        steps: Mapping[str, _EventRecord],
        decisions: Mapping[str, _EventRecord],
    ) -> None:
        payload = transition.payload
        if payload.get("post_observation") is not None:
            self._record_error(
                "terminal_transition_has_post_observation",
                "transition_not_executed must have a null post_observation",
                transition,
            )
        step = steps.get(str(payload.get("step_id")))
        decision = decisions.get(str(payload.get("decision_id")))
        if step is None or payload.get("pre_observation_event_id") != step.event_id:
            self._record_error(
                "terminal_pre_observation_reference",
                "transition_not_executed must link the exact step_started event",
                transition,
            )
        if decision is None or decision.payload.get("step_id") != payload.get("step_id"):
            self._record_error(
                "terminal_decision_reference",
                "transition_not_executed must link the exact decision",
                transition,
            )

    def _validate_max_step_transitions(
        self,
        decisions: Mapping[str, _EventRecord],
        executions: Mapping[str, _EventRecord],
        terminals: Mapping[str, Sequence[_EventRecord]],
    ) -> None:
        for task_end in self._records:
            if task_end.event_type != "task_ended":
                continue
            termination = task_end.payload.get("termination")
            if not isinstance(termination, Mapping) or termination.get("source") != "max_step":
                continue
            step_index = termination.get("step_index")
            task_decisions = [
                decision
                for decision in decisions.values()
                if decision.task_run_id == task_end.task_run_id
                and self._step_index_for_decision(decision) == step_index
            ]
            for decision in task_decisions:
                task_executions = [
                    execution
                    for execution in executions.values()
                    if execution.payload.get("decision_id") == decision.payload.get("decision_id")
                ]
                for execution in task_executions:
                    outcomes = terminals.get(str(execution.payload.get("execution_id")), [])
                    if not outcomes or any(
                        outcome.seq is not None
                        and task_end.seq is not None
                        and outcome.seq >= task_end.seq
                        for outcome in outcomes
                    ):
                        self._record_error(
                            "max_step_transition_missing",
                            "executed max-step action must close before task_ended",
                            task_end,
                        )

    def _step_index_for_decision(self, decision: _EventRecord) -> Any:
        step_id = decision.payload.get("step_id")
        for record in self._records:
            if record.event_type == "step_started" and record.payload.get("step_id") == step_id:
                return record.payload.get("step_index")
        return None

    @staticmethod
    def _is_earlier_same_task(first: _EventRecord, second: _EventRecord) -> bool:
        return (
            first.expected_task_run_id == second.expected_task_run_id
            and first.seq is not None
            and second.seq is not None
            and first.seq < second.seq
        )

    def _collect_and_validate_blobs(self) -> None:
        for name, manifest in self._manifests.items():
            self._walk_for_blobs(
                manifest,
                source=self.run_root / name,
                json_path="$",
                task_run_id=None,
            )
        for record in self._records:
            self._walk_for_blobs(
                record.event,
                source=record.source,
                json_path=f"$[{record.line}]",
                task_run_id=record.task_run_id,
            )

        queue = list(self._blob_occurrences)
        checked_occurrences = 0
        parsed_json_blobs: set[str] = set()
        while queue:
            occurrence = queue.pop(0)
            checked_occurrences += 1
            data = self._validate_blob_occurrence(occurrence)
            if data is None:
                continue
            relative_path = occurrence.reference.get("relative_path")
            media_type = occurrence.reference.get("media_type")
            if (
                isinstance(relative_path, str)
                and relative_path not in parsed_json_blobs
                and isinstance(media_type, str)
                and (media_type.endswith("/json") or media_type.endswith("+json"))
            ):
                parsed_json_blobs.add(relative_path)
                try:
                    nested = _loads_strict(data)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    self._error(
                        "invalid_json_blob",
                        f"JSON media-type blob is not valid strict JSON: {error}",
                        source=self.run_root / relative_path,
                        task_run_id=occurrence.task_run_id,
                    )
                else:
                    before = len(self._blob_occurrences)
                    self._walk_for_blobs(
                        nested,
                        source=self.run_root / relative_path,
                        json_path="$",
                        task_run_id=occurrence.task_run_id,
                    )
                    queue.extend(self._blob_occurrences[before:])

        all_blob_paths = {
            path.relative_to(self.run_root).as_posix()
            for path in self._iter_blob_files()
            if path.is_file()
        }
        for orphan in sorted(all_blob_paths - self._verified_blob_paths):
            self._warning(
                "orphan_blob",
                "blob is not reachable from any manifest or event reference",
                source=self.run_root / orphan,
            )
        self.counts["blob_reference_count"] = checked_occurrences
        self.counts["verified_blob_count"] = len(self._verified_blob_paths)
        self.counts["orphan_blob_count"] = len(all_blob_paths - self._verified_blob_paths)

    def _walk_for_blobs(
        self,
        value: Any,
        *,
        source: Path,
        json_path: str,
        task_run_id: str | None,
    ) -> None:
        if isinstance(value, Mapping):
            keys = frozenset(value)
            if keys == _BLOB_FIELDS:
                occurrence = _BlobOccurrence(dict(value), source, json_path, task_run_id)
                self._blob_occurrences.append(occurrence)
                return
            if {"digest", "relative_path"}.issubset(keys) and keys != _BLOB_FIELDS:
                self._error(
                    "invalid_blob_reference_shape",
                    "blob reference must contain exactly the v1 BlobRef fields",
                    source=source,
                    task_run_id=task_run_id,
                    json_path=json_path,
                )
            for key, child in value.items():
                self._walk_for_blobs(
                    child,
                    source=source,
                    json_path=f"{json_path}.{key}",
                    task_run_id=task_run_id,
                )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, child in enumerate(value):
                self._walk_for_blobs(
                    child,
                    source=source,
                    json_path=f"{json_path}[{index}]",
                    task_run_id=task_run_id,
                )

    def _validate_blob_occurrence(self, occurrence: _BlobOccurrence) -> bytes | None:
        reference = occurrence.reference
        digest = reference.get("digest")
        relative_path = reference.get("relative_path")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            self._error(
                "invalid_blob_digest",
                "blob reference digest must be lowercase SHA-256",
                source=occurrence.source,
                task_run_id=occurrence.task_run_id,
                json_path=occurrence.json_path,
            )
            return None
        if not isinstance(relative_path, str):
            self._error(
                "invalid_blob_path",
                "blob reference relative_path must be a string",
                source=occurrence.source,
                task_run_id=occurrence.task_run_id,
                json_path=occurrence.json_path,
            )
            return None
        pure_path = PurePosixPath(relative_path)
        if pure_path.is_absolute() or ".." in pure_path.parts:
            self._error(
                "unsafe_blob_path",
                "blob reference must remain beneath the run root",
                source=occurrence.source,
                task_run_id=occurrence.task_run_id,
                json_path=occurrence.json_path,
            )
            return None
        path = self.run_root.joinpath(*pure_path.parts)
        if path.is_symlink():
            self._error(
                "blob_symlink",
                "blob evidence must not be a symbolic link",
                source=path,
                task_run_id=occurrence.task_run_id,
            )
            return None
        try:
            typed_reference: BlobRef = reference  # type: ignore[assignment]
            data = self.blob_store.read_bytes(typed_reference)
        except (BlobIntegrityError, KeyError, OSError, TypeError, ValueError) as error:
            self._error(
                "blob_integrity",
                str(error),
                source=occurrence.source,
                task_run_id=occurrence.task_run_id,
                json_path=occurrence.json_path,
            )
            return None
        self._verified_blob_paths.add(relative_path)
        if reference.get("media_type") == ARTIFACT_GRAPH_MEDIA_TYPE:
            try:
                graph = self.artifact_serializer.load_graph(typed_reference)
                for nested_reference in _iter_blob_references(graph):
                    self.blob_store.verify(nested_reference)
                self.artifact_serializer.rehydrate(graph)
            except (
                BlobIntegrityError,
                KeyError,
                OSError,
                SerializationError,
                TypeError,
                ValueError,
            ) as error:
                self._error(
                    "artifact_graph_integrity",
                    f"authoritative artifact graph cannot be rehydrated: {error}",
                    source=occurrence.source,
                    task_run_id=occurrence.task_run_id,
                    json_path=occurrence.json_path,
                )
        return data

    def _scan_all_evidence_for_secrets(self) -> None:
        if not self._secrets:
            return
        paths = [
            self.run_root / "manifest.start.json",
            self.run_root / "manifest.final.json",
            self.run_root / "run.events.jsonl",
        ]
        paths.extend(record.source for record in self._records)
        paths.extend(self._iter_blob_files())
        for path in sorted(set(paths)):
            if not path.is_file() or path.name == "integrity_report.json":
                continue
            try:
                data = path.read_bytes()
            except OSError as error:
                self._error("secret_scan_read", str(error), source=path)
                continue
            for fingerprint, secret in self._secrets:
                if secret in data:
                    self._secret_error(path, fingerprint)

        # JSON escaping can hide an exact semantic string from a raw byte scan.
        for name, manifest in self._manifests.items():
            self._scan_semantic_secret_values(manifest, self.run_root / name)
        for record in self._records:
            self._scan_semantic_secret_values(record.event, record.source, record.task_run_id)

    def _scan_semantic_secret_values(
        self,
        value: Any,
        source: Path,
        task_run_id: str | None = None,
    ) -> None:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            for fingerprint, secret in self._secrets:
                if secret in encoded:
                    self._secret_error(source, fingerprint, task_run_id)
        elif isinstance(value, Mapping):
            for key, child in value.items():
                self._scan_semantic_secret_values(key, source, task_run_id)
                self._scan_semantic_secret_values(child, source, task_run_id)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for child in value:
                self._scan_semantic_secret_values(child, source, task_run_id)

    def _secret_error(self, source: Path, fingerprint: str, task_run_id: str | None = None) -> None:
        relative = self._relative(source)
        key = (relative, fingerprint)
        if key in self._secret_hits:
            return
        self._secret_hits.add(key)
        self._error(
            "configured_secret_present",
            "a configured secret exact value appears in raw evidence",
            source=source,
            task_run_id=task_run_id,
            secret_fingerprint=fingerprint,
        )

    def _scan_credential_keys(
        self,
        value: Any,
        *,
        source: Path,
        json_path: str,
        task_run_id: str | None = None,
        opaque_keys: frozenset[str] = frozenset(),
    ) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                normalized = re.sub(r"[^a-z0-9]+", "_", key_text.casefold()).strip("_")
                if normalized in _CREDENTIAL_KEYS:
                    self._error(
                        "credential_key_present",
                        "credential-bearing key is forbidden in raw evidence",
                        source=source,
                        task_run_id=task_run_id,
                        json_path=f"{json_path}.{key_text}",
                    )
                if key_text.casefold() not in opaque_keys:
                    self._scan_credential_keys(
                        child,
                        source=source,
                        json_path=f"{json_path}.{key_text}",
                        task_run_id=task_run_id,
                        opaque_keys=opaque_keys,
                    )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, child in enumerate(value):
                self._scan_credential_keys(
                    child,
                    source=source,
                    json_path=f"{json_path}[{index}]",
                    task_run_id=task_run_id,
                    opaque_keys=opaque_keys,
                )

    def _validate_capture_complete(self) -> None:
        task_ends: dict[str, _EventRecord] = {}
        collector_error_tasks: set[str | None] = set()
        for record in self._records:
            if record.event_type == "task_ended" and record.task_run_id is not None:
                task_ends[record.task_run_id] = record
            elif record.event_type == "collector_error":
                collector_error_tasks.add(record.task_run_id)

        prior_errors = list(self.errors)
        for task_run_id, task_end in task_ends.items():
            if task_end.payload.get("capture_complete") is not True:
                continue
            missing = task_end.payload.get("missing_artifacts")
            has_missing = (
                isinstance(missing, Sequence) and not isinstance(missing, str) and bool(missing)
            )
            has_task_errors = any(error.get("task_run_id") == task_run_id for error in prior_errors)
            if task_run_id in collector_error_tasks or has_missing or has_task_errors:
                self._record_error(
                    "task_capture_complete_inconsistent",
                    "task capture_complete cannot be true when required evidence is missing",
                    task_end,
                )

        final = self._manifests.get("manifest.final.json")
        if final is not None and final.get("capture_complete") is True and prior_errors:
            self._error(
                "run_capture_complete_inconsistent",
                "final manifest capture_complete cannot be true when integrity errors exist",
                source=self.run_root / "manifest.final.json",
            )

    def _finish_counts(self) -> None:
        self.counts["error_count"] = len(self.errors)
        self.counts["warning_count"] = len(self.warnings)
        self.counts["manifest_count"] = len(self._manifests)
        self.counts["configured_secret_count"] = len(self._secrets)

    def _read_json_document(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            self._error("missing_manifest", "required manifest is missing", source=path)
            return None
        try:
            value = _loads_strict(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._error("invalid_manifest_json", str(error), source=path)
            return None
        if not isinstance(value, dict):
            self._error("invalid_manifest_shape", "manifest must be a JSON object", source=path)
            return None
        return value

    def _read_jsonl(self, path: Path, *, expected_task_run_id: str | None) -> list[_EventRecord]:
        if not path.is_file():
            self._error(
                "missing_event_stream",
                "required event stream is missing",
                source=path,
                task_run_id=expected_task_run_id,
            )
            return []
        try:
            data = path.read_bytes()
        except OSError as error:
            self._error(
                "event_stream_read",
                str(error),
                source=path,
                task_run_id=expected_task_run_id,
            )
            return []
        if data and not data.endswith(b"\n"):
            self._error(
                "incomplete_jsonl_tail",
                "JSONL stream does not end with a complete newline-delimited record",
                source=path,
                task_run_id=expected_task_run_id,
            )

        records: list[_EventRecord] = []
        for line_number, raw_line in enumerate(data.splitlines(), start=1):
            if not raw_line.strip():
                self._error(
                    "empty_jsonl_record",
                    "JSONL streams must not contain empty records",
                    source=path,
                    line=line_number,
                    task_run_id=expected_task_run_id,
                )
                continue
            try:
                value = _loads_strict(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._error(
                    "invalid_jsonl_record",
                    str(error),
                    source=path,
                    line=line_number,
                    task_run_id=expected_task_run_id,
                )
                continue
            if not isinstance(value, dict):
                self._error(
                    "invalid_event_shape",
                    "each JSONL record must be an object",
                    source=path,
                    line=line_number,
                    task_run_id=expected_task_run_id,
                )
                continue
            records.append(_EventRecord(value, path, line_number, expected_task_run_id))
        return records

    def _iter_blob_files(self) -> list[Path]:
        root = self.run_root / "blobs" / "sha256"
        if not root.exists():
            return []
        if not root.is_dir():
            self._error("blob_root_not_directory", "blob root is not a directory", source=root)
            return []
        return [path for path in root.rglob("*") if path.is_file() or path.is_symlink()]

    def _record_error(
        self,
        code: str,
        message: str,
        record: _EventRecord,
        **context: Any,
    ) -> None:
        self._error(
            code,
            message,
            source=record.source,
            line=record.line,
            task_run_id=record.task_run_id,
            event_id=record.event_id,
            **context,
        )

    def _error(
        self,
        code: str,
        message: str,
        *,
        source: Path | None = None,
        line: int | None = None,
        task_run_id: str | None = None,
        **context: Any,
    ) -> None:
        issue: dict[str, Any] = {"code": code, "message": message}
        if source is not None:
            issue["source"] = self._relative(source)
        if line is not None:
            issue["line"] = line
        if task_run_id is not None:
            issue["task_run_id"] = task_run_id
        issue.update({key: value for key, value in context.items() if value is not None})
        self.errors.append(issue)

    def _warning(
        self,
        code: str,
        message: str,
        *,
        source: Path | None = None,
        **context: Any,
    ) -> None:
        issue: dict[str, Any] = {"code": code, "message": message}
        if source is not None:
            issue["source"] = self._relative(source)
        issue.update(context)
        self.warnings.append(issue)

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.run_root).as_posix()
        except ValueError:
            return str(path)


def check_run_integrity(
    run_root: str | os.PathLike[str],
    *,
    configured_secrets: Iterable[str | bytes] = (),
    write_report: bool = False,
) -> dict[str, Any]:
    """Validate one run, optionally creating ``integrity_report.json``."""

    checker = IntegrityChecker(run_root, configured_secrets=configured_secrets)
    report = checker.check()
    if write_report:
        checker.write_report(report)
    return report


def _normalize_secrets(values: Iterable[str | bytes]) -> tuple[tuple[str, bytes], ...]:
    normalized: dict[bytes, str] = {}
    for value in values:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
        elif isinstance(value, bytes):
            encoded = value
        else:
            raise TypeError("configured secrets must be strings or bytes")
        if not encoded or is_placeholder_credential(encoded):
            continue
        normalized.setdefault(encoded, hashlib.sha256(encoded).hexdigest()[:12])
    return tuple((fingerprint, secret) for secret, fingerprint in normalized.items())


def _iter_blob_references(value: Any) -> Iterator[BlobRef]:
    if isinstance(value, Mapping):
        if frozenset(value) == _BLOB_FIELDS:
            yield dict(value)  # type: ignore[misc]
            return
        for child in value.values():
            yield from _iter_blob_references(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _iter_blob_references(child)


def _is_blob_reference(value: Any) -> bool:
    return isinstance(value, Mapping) and frozenset(value) == _BLOB_FIELDS


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_optional_mapping(value: Any) -> bool:
    return value is None or isinstance(value, Mapping)


def _is_string_list(value: Any, *, unique: bool = False) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and (not unique or len(value) == len(set(value)))
    )


def _file_summary(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return {"sha256": digest.hexdigest(), "byte_count": byte_count}


def _mapping_node(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SerializationError(f"{description} must be an object")
    return value


def _mapping_sequence(value: Any, description: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise SerializationError(f"{description} must be an object list")
    return value


def _loads_strict(data: bytes) -> Any:
    text = data.decode("utf-8")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise _DuplicateJsonKey(f"duplicate JSON object key: {key!r}")
            value[key] = child
        return value

    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda constant: (_raise_nonfinite(constant)),
    )


def _raise_nonfinite(constant: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {constant}")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_value(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("zero-byte write while creating integrity report")
        view = view[written:]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CHECKER_VERSION", "IntegrityChecker", "check_run_integrity"]
