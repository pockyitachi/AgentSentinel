"""Feature-gated run and task-attempt lifecycle for raw audit collection.

The disabled path returns before repository inspection, ID allocation, secret
normalization, serialization, or filesystem access.  The enabled path owns one
``RunRecorder`` and hands runner code one independently-scoped task recorder
for every physical whole-task attempt.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import os
import platform
import re
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mobile_world.runtime.audit.config import AuditConfig, CollectorMode
from mobile_world.runtime.audit.null_recorder import NULL_RECORDER
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import (
    RunnerTaskCapture,
    RunnerTaskMetadata,
)
from mobile_world.runtime.audit.schemas import SCHEMA_VERSION, Producer
from mobile_world.runtime.audit.secret_policy import is_placeholder_credential

_REPOSITORY = "pockyitachi/AgentSentinel"
_REPOSITORY_URL = "https://github.com/pockyitachi/AgentSentinel.git"
_REDACTED = "[REDACTED_CONFIGURED_SECRET]"
_OMIT = object()
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
_CREDENTIAL_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_refresh_token",
    "_client_secret",
    "_password",
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "credential",
        "key",
        "password",
        "signature",
        "sig",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class TaskAuditBinding:
    """Objects injected into one physical runner task attempt."""

    task_recorder: TaskRecorder
    capture: RunnerTaskCapture
    metadata: RunnerTaskMetadata
    store_stream_chunks: bool


@dataclass(frozen=True, slots=True)
class _TaskOutcome:
    runtime_status: str
    retry_planned: bool


class NullAuditLifecycle:
    """True no-op lifecycle used by the default-disabled CLI path."""

    __slots__ = ()
    enabled = False
    degraded = False
    capture_complete = True
    missing_artifacts: tuple[str, ...] = ()
    run_id = None
    recorder = NULL_RECORDER

    def start_task_attempt(self, *args: Any, **kwargs: Any) -> None:
        return None

    def finish_task_attempt(self, *args: Any, **kwargs: Any) -> None:
        return None

    def finalize(self, *args: Any, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> NullAuditLifecycle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


NULL_AUDIT_LIFECYCLE = NullAuditLifecycle()


class DegradedAuditLifecycle(NullAuditLifecycle):
    """In-memory fail-open result when enabled storage cannot initialize."""

    __slots__ = ()
    degraded = True
    capture_complete = False
    missing_artifacts = ("audit_bootstrap",)


DEGRADED_AUDIT_LIFECYCLE = DegradedAuditLifecycle()


class AuditLifecycle:
    """Own one enabled run and its physical task-attempt bindings."""

    enabled = True

    def __init__(
        self,
        recorder: RunRecorder,
        *,
        configured_secrets: Iterable[str | bytes] = (),
        run_started_event_id: str,
        store_stream_chunks: bool = True,
        initial_missing_artifacts: Iterable[str] = (),
    ) -> None:
        self.recorder = recorder
        self.run_id = recorder.run_id
        self._configured_secrets = tuple(configured_secrets)
        self._run_started_event_id = run_started_event_id
        self._store_stream_chunks = store_stream_chunks
        self._bindings: dict[str, TaskAuditBinding] = {}
        self._outcomes: dict[str, _TaskOutcome] = {}
        self._attempt_counts: dict[str, int] = {}
        self._run_collector_error_event_ids: list[str] = []
        self._run_missing_artifacts = _ordered_unique(
            [
                *initial_missing_artifacts,
                *([] if store_stream_chunks else ["model_stream_chunks"]),
            ]
        )
        self._lock = threading.RLock()
        self._finalizing = False
        self._finalized = False

    def start_task_attempt(
        self,
        *,
        task_name: str,
        task_index: int,
        suite_family: str,
        agent: Any,
        environment: Any,
        whole_task_attempt_index: int,
    ) -> TaskAuditBinding | None:
        """Allocate one new task stream for one physical whole-task attempt."""

        try:
            with self._lock:
                if self._finalizing or self._finalized:
                    raise RuntimeError("audit run is finalizing or finalized")
                if not isinstance(task_name, str) or not task_name:
                    raise ValueError("task_name must be a non-empty string")
                if (
                    isinstance(whole_task_attempt_index, bool)
                    or not isinstance(whole_task_attempt_index, int)
                    or whole_task_attempt_index < 1
                ):
                    raise ValueError("whole_task_attempt_index must be a positive integer")
                effective_attempt_index = max(
                    self._attempt_counts.get(task_name, 0) + 1,
                    whole_task_attempt_index,
                )
                self._attempt_counts[task_name] = effective_attempt_index
                task_recorder = self.recorder.open_task()
                capture = RunnerTaskCapture(
                    task_recorder,
                    configured_secrets=self._configured_secrets,
                )
                metadata = RunnerTaskMetadata(
                    run_id=self.run_id,
                    task_run_id=task_recorder.task_run_id,
                    task_index=task_index,
                    suite_family=suite_family,
                    agent=_agent_metadata(agent, self._configured_secrets),
                    environment=_environment_metadata(
                        environment,
                        self._configured_secrets,
                    ),
                    whole_task_attempt_index=effective_attempt_index,
                    store_stream_chunks=self._store_stream_chunks,
                )
                binding = TaskAuditBinding(
                    task_recorder,
                    capture,
                    metadata,
                    self._store_stream_chunks,
                )
                if not self._store_stream_chunks:
                    capture.mark_incomplete("model_stream_chunks")
                self._bindings[task_recorder.task_run_id] = binding
                return binding
        except Exception as error:
            return self._task_lifecycle_failure(
                scope="start_task_attempt",
                missing="task_stream",
                error=error,
            )

    def finish_task_attempt(
        self,
        *,
        binding: TaskAuditBinding | None,
        result: tuple[int, float] | None,
        exception: BaseException | None,
        retry_planned: bool,
        runtime_status: str,
    ) -> None:
        """Close a task writer using the status emitted by the audited runner."""

        if binding is None:
            return
        try:
            if runtime_status not in {"completed", "aborted", "crashed"}:
                raise ValueError("runtime_status must be completed, aborted, or crashed")
            with self._lock:
                self._outcomes[binding.metadata.task_run_id] = _TaskOutcome(
                    runtime_status=runtime_status,
                    retry_planned=bool(retry_planned),
                )
            binding.task_recorder.close()
        except Exception as error:
            self._task_lifecycle_failure(
                scope="finish_task_attempt",
                missing="task_stream_close",
                error=error,
                task_recorder=binding.task_recorder,
            )

    def finalize(self, *, runtime_status: str = "completed") -> Path | None:
        """Append ``run_ended`` and exclusively create ``manifest.final.json``."""

        if runtime_status not in {"completed", "aborted", "crashed"}:
            self._remember_missing_best_effort("run_finalization_status")
            return None
        with self._lock:
            if self._finalized:
                return self.recorder.manifest_final_path
            if self._finalizing:
                return None
            self._finalizing = True
            bindings = tuple(self._bindings.values())

        try:
            for binding in bindings:
                task_run_id = binding.metadata.task_run_id
                if task_run_id not in self._outcomes:
                    binding.capture.mark_incomplete("task_attempt_finalization")
                    self._outcomes[task_run_id] = _TaskOutcome(
                        runtime_status="crashed",
                        retry_planned=False,
                    )
                binding.task_recorder.close()

            task_summaries = [self._task_summary(binding) for binding in bindings]
            collector_error_ids = _ordered_unique(
                [
                    *self._run_collector_error_event_ids,
                    *(
                        event_id
                        for summary in task_summaries
                        for event_id in summary["collector_error_event_ids"]
                    ),
                ]
            )
            capture_complete = not self._run_missing_artifacts and all(
                summary["capture_complete"] for summary in task_summaries
            )
            completed = sum(summary["runtime_status"] == "completed" for summary in task_summaries)
            crashed = len(task_summaries) - completed
            self.recorder.append_run_event(
                "run_ended",
                {
                    "runtime_status": runtime_status,
                    "task_run_ids": [summary["task_run_id"] for summary in task_summaries],
                    "task_counts": {
                        "started": len(task_summaries),
                        "completed": completed,
                        "crashed": crashed,
                    },
                    "capture_complete": capture_complete,
                    "collector_error_event_ids": collector_error_ids,
                    "manifest_final_path": "manifest.final.json",
                },
                caused_by_event_id=self._run_started_event_id,
            )

            run_stream = _file_summary(self.recorder.run_root / "run.events.jsonl")
            start_manifest = _file_summary(self.recorder.manifest_start_path)
            blob_summary = _blob_summary(self.recorder.run_root / "blobs" / "sha256")
            final_manifest = {
                "raw_schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "ended_at_utc": _utc_now(),
                "runtime_status": runtime_status,
                "manifest_start": start_manifest,
                "run_stream": run_stream,
                "task_streams": task_summaries,
                "blob_count": blob_summary["count"],
                "blob_byte_count": blob_summary["byte_count"],
                "capture_complete": capture_complete,
                "missing_artifacts": list(self._run_missing_artifacts),
                "collector_error_event_ids": collector_error_ids,
            }
            path = self.recorder.write_manifest_final(final_manifest)
        except Exception:
            with self._lock:
                self._finalizing = False
            self._remember_missing_best_effort("manifest.final.json")
            _close_best_effort(self.recorder)
            return None

        with self._lock:
            self._finalized = True
            self._finalizing = False
        _close_best_effort(self.recorder)
        return path

    def close(self) -> None:
        """Close writers without fabricating a terminal lifecycle event."""

        _close_best_effort(self.recorder)

    def __enter__(self) -> AuditLifecycle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.finalize(runtime_status="crashed" if exc_type is not None else "completed")

    def _task_summary(self, binding: TaskAuditBinding) -> dict[str, Any]:
        task_run_id = binding.metadata.task_run_id
        outcome = self._outcomes.get(
            task_run_id,
            _TaskOutcome(runtime_status="crashed", retry_planned=False),
        )
        recorder = binding.task_recorder
        capture_complete = bool(binding.capture.capture_complete and recorder.capture_complete)
        missing = _ordered_unique([*binding.capture.missing_artifacts, *recorder.missing_artifacts])
        error_ids = _ordered_unique(
            [
                *binding.capture.collector_error_event_ids,
                *recorder.collector_error_event_ids,
            ]
        )
        return {
            "task_run_id": task_run_id,
            "relative_path": recorder.path.relative_to(self.recorder.run_root).as_posix(),
            **_file_summary(recorder.path),
            "runtime_status": outcome.runtime_status,
            "retry_planned": outcome.retry_planned,
            "capture_complete": capture_complete and not missing,
            "missing_artifacts": missing,
            "collector_error_event_ids": error_ids,
        }

    def _task_lifecycle_failure(
        self,
        *,
        scope: str,
        missing: str,
        error: Exception,
        task_recorder: TaskRecorder | None = None,
    ) -> None:
        self._remember_missing_best_effort(missing)
        if task_recorder is not None:
            try:
                task_recorder.mark_incomplete(missing)
            except Exception:
                pass
        try:
            self._append_run_collector_error(scope=scope, missing=missing, error=error)
        except Exception:
            pass
        return None

    def _append_run_collector_error(
        self,
        *,
        scope: str,
        missing: str,
        error: Exception,
    ) -> None:
        try:
            event = self.recorder.append_run_event(
                "collector_error",
                {
                    "scope": scope,
                    "related_event_id": self._run_started_event_id,
                    "step_id": None,
                    "exception": {
                        "class": f"{type(error).__module__}.{type(error).__qualname__}",
                        "message": _scrub_text(str(error), self._configured_secrets),
                        "details_blob": None,
                    },
                    "missing_artifacts": [missing],
                    "agent_execution_continued": True,
                },
                caused_by_event_id=self._run_started_event_id,
            )
        except Exception:
            return
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            with self._lock:
                if event_id not in self._run_collector_error_event_ids:
                    self._run_collector_error_event_ids.append(event_id)

    def _remember_missing(self, artifact: str) -> None:
        with self._lock:
            if artifact not in self._run_missing_artifacts:
                self._run_missing_artifacts.append(artifact)

    def _remember_missing_best_effort(self, artifact: str) -> None:
        try:
            self._remember_missing(artifact)
        except Exception:
            pass


def bootstrap_audit_run(
    config: AuditConfig,
    *,
    repository_root: str | Path | None = None,
    repository: str = _REPOSITORY,
    repository_url: str = _REPOSITORY_URL,
    repository_commit: str | None = None,
    repository_dirty: bool | None = None,
    mobile_world_upstream_url: str | None = None,
    mobile_world_upstream_commit: str | None = None,
    resolved_cli_config: Mapping[str, Any] | None = None,
    resolved_agent_runtime_config: Mapping[str, Any] | None = None,
    agent_type: str | None = None,
    model_name: str | None = None,
    suite_family: str = "mobile_world",
    environment_image: str | None = None,
    configured_secrets: Iterable[str | bytes] = (),
    worker_id: str = "eval-cli",
    sync: bool = True,
) -> AuditLifecycle | NullAuditLifecycle | DegradedAuditLifecycle:
    """Create a passive runtime lifecycle without surfacing collector failures.

    The real eval/runtime lifecycle is fixed to fail-open.  The legacy ``sync``
    keyword is accepted for source compatibility and ignored; per-event fsync
    is never enabled on the runner's critical path.
    """

    if not config.enabled:
        return NULL_AUDIT_LIFECYCLE
    if config.collector_mode is not CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER:
        return DEGRADED_AUDIT_LIFECYCLE
    del sync
    try:
        return _bootstrap_enabled_audit_run(
            config,
            repository_root=repository_root,
            repository=repository,
            repository_url=repository_url,
            repository_commit=repository_commit,
            repository_dirty=repository_dirty,
            mobile_world_upstream_url=mobile_world_upstream_url,
            mobile_world_upstream_commit=mobile_world_upstream_commit,
            resolved_cli_config=resolved_cli_config,
            resolved_agent_runtime_config=resolved_agent_runtime_config,
            agent_type=agent_type,
            model_name=model_name,
            suite_family=suite_family,
            environment_image=environment_image,
            configured_secrets=configured_secrets,
            worker_id=worker_id,
        )
    except Exception:
        return DEGRADED_AUDIT_LIFECYCLE


def _bootstrap_enabled_audit_run(
    config: AuditConfig,
    *,
    repository_root: str | Path | None = None,
    repository: str = _REPOSITORY,
    repository_url: str = _REPOSITORY_URL,
    repository_commit: str | None = None,
    repository_dirty: bool | None = None,
    mobile_world_upstream_url: str | None = None,
    mobile_world_upstream_commit: str | None = None,
    resolved_cli_config: Mapping[str, Any] | None = None,
    resolved_agent_runtime_config: Mapping[str, Any] | None = None,
    agent_type: str | None = None,
    model_name: str | None = None,
    suite_family: str = "mobile_world",
    environment_image: str | None = None,
    configured_secrets: Iterable[str | bytes] = (),
    worker_id: str = "eval-cli",
) -> AuditLifecycle | NullAuditLifecycle | DegradedAuditLifecycle:
    """Create one run, or return the zero-work disabled singleton.

    The first branch is intentionally before all other argument inspection.
    Callers may therefore pass lazy/poisoned metadata in parity tests and prove
    that the default-off path performs no collection work.
    """

    if not config.enabled:
        return NULL_AUDIT_LIFECYCLE

    repo_root = (
        Path(repository_root).expanduser().resolve(strict=False)
        if repository_root is not None
        else _find_repository_root(Path(__file__))
    )
    audit_root = config.validated_external_log_root(repo_root)
    commit = (repository_commit or _read_git_commit(repo_root)).lower()
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("repository commit must be 40 lowercase hexadecimal characters")
    if repository_dirty is not None and not isinstance(repository_dirty, bool):
        raise TypeError("repository_dirty must be a bool or None")
    if (mobile_world_upstream_url is None) != (mobile_world_upstream_commit is None):
        raise ValueError("MobileWorld upstream URL and commit must be supplied together")
    if mobile_world_upstream_url is None:
        upstream = _read_mobileworld_upstream(repo_root)
    else:
        upstream = {
            "repository_url": _validated_provenance_url(mobile_world_upstream_url),
            "commit": _validated_commit(mobile_world_upstream_commit),
            "provenance_file": None,
        }

    supplied_secrets = _normalize_configured_secrets(configured_secrets)
    cli_value, cli_excluded, cli_discovered = sanitize_collector_config(
        resolved_cli_config or {},
        configured_secrets=supplied_secrets,
        root_path="resolved_cli_config",
    )
    runtime_value, runtime_excluded, runtime_discovered = sanitize_collector_config(
        resolved_agent_runtime_config or {},
        configured_secrets=(*supplied_secrets, *cli_discovered),
        root_path="resolved_agent_runtime_config",
    )
    all_secrets = _normalize_configured_secrets(
        (*supplied_secrets, *cli_discovered, *runtime_discovered)
    )
    safe_repository = _scrub_text(repository, all_secrets)
    safe_repository_url = _credential_free_url(repository_url, all_secrets)
    safe_agent_type = _scrub_optional_text(agent_type, all_secrets)
    safe_model_name = _scrub_optional_text(model_name, all_secrets)
    safe_suite_family = _scrub_text(suite_family, all_secrets)
    safe_environment_image = _scrub_optional_text(environment_image, all_secrets)
    direct_excluded = [
        f"manifest.{field}"
        for field, original, sanitized in (
            ("repository", repository, safe_repository),
            ("repository_url", repository_url, safe_repository_url),
            ("agent_type", agent_type, safe_agent_type),
            ("model_name", model_name, safe_model_name),
            ("suite_family", suite_family, safe_suite_family),
            ("environment_image", environment_image, safe_environment_image),
        )
        if original != sanitized
    ]
    excluded_fields = _ordered_unique([*cli_excluded, *runtime_excluded, *direct_excluded])
    mobile_world_version = _package_version("mobile-world")
    openai_version = _package_version("openai")
    provider_sdk_configuration = _provider_sdk_configuration(
        safe_agent_type,
        openai_version,
    )
    started_at = _utc_now()
    producer = Producer.local(
        version=mobile_world_version or "unknown",
        worker_id=worker_id,
    )
    try:
        recorder = RunRecorder(
            audit_root,
            producer=producer,
            collector_mode=CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER,
            sync=False,
        )
    except Exception:
        return DEGRADED_AUDIT_LIFECYCLE
    manifest = {
        "raw_schema_version": SCHEMA_VERSION,
        "run_id": recorder.run_id,
        "repository": safe_repository,
        "repository_url": safe_repository_url,
        "git_commit": commit,
        "git_dirty": repository_dirty,
        "git_dirty_status": "reported" if repository_dirty is not None else "not_checked",
        "monorepo": {
            "repository": safe_repository,
            "repository_url": safe_repository_url,
            "commit": commit,
            "dirty": repository_dirty,
            "dirty_status": ("reported" if repository_dirty is not None else "not_checked"),
        },
        "mobile_world_snapshot": {
            "path": "MobileWorld",
            "upstream_repository_url": upstream["repository_url"],
            "upstream_commit": upstream["commit"],
            "provenance_file": upstream["provenance_file"],
        },
        "python_version": platform.python_version(),
        "mobile_world_version": mobile_world_version,
        "agent_type": safe_agent_type,
        "model_name": safe_model_name,
        "suite_family": safe_suite_family,
        "resolved_cli_config": cli_value,
        "resolved_agent_runtime_config": runtime_value,
        "environment_image": safe_environment_image,
        "provider_sdk_configuration": provider_sdk_configuration,
        "started_at_utc": started_at,
        "collection_policy": {
            "label_free": True,
            "prompt_intervention": False,
            "collector_mode": CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER.value,
            "stream_chunks": config.store_stream_chunks,
        },
        "excluded_secret_fields": excluded_fields,
    }
    try:
        recorder.write_manifest_start(manifest)
        run_started = recorder.append_run_event(
            "run_started",
            {
                "collector_mode": CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER.value,
                "repository": {
                    "url": safe_repository_url,
                    "commit": commit,
                    "dirty": repository_dirty,
                    "diff_blob": None,
                },
                "mobile_world_snapshot": {
                    "path": "MobileWorld",
                    "upstream_repository_url": upstream["repository_url"],
                    "upstream_commit": upstream["commit"],
                    "provenance_file": upstream["provenance_file"],
                },
                "runtime": {
                    "python": sys.version.split()[0],
                    "mobileworld": mobile_world_version,
                    "openai_sdk": openai_version,
                    "provider_sdk_configuration": provider_sdk_configuration,
                    "platform": platform.platform(),
                },
                "configuration": {
                    "suite_family": safe_suite_family,
                    "agent_type": safe_agent_type,
                    "model_name": safe_model_name,
                    "audit_enabled": True,
                    "additional_config": runtime_value,
                },
                "excluded_secrets": excluded_fields,
            },
        )
    except Exception:
        _close_best_effort(recorder)
        return DEGRADED_AUDIT_LIFECYCLE
    except BaseException:
        _close_best_effort(recorder)
        raise
    return AuditLifecycle(
        recorder,
        configured_secrets=all_secrets,
        run_started_event_id=run_started["event_id"],
        store_stream_chunks=config.store_stream_chunks,
        initial_missing_artifacts=(
            () if repository_dirty is not None else ("repository_dirty_state",)
        ),
    )


def sanitize_collector_config(
    value: Mapping[str, Any],
    *,
    configured_secrets: Iterable[str | bytes] = (),
    root_path: str = "configuration",
) -> tuple[dict[str, Any], list[str], tuple[str | bytes, ...]]:
    """Return JSON-safe config, excluded paths, and newly found secret values."""

    if not isinstance(value, Mapping):
        raise TypeError("collector configuration metadata must be a mapping")
    secrets = list(_normalize_configured_secrets(configured_secrets))
    discovered: list[str | bytes] = []
    excluded: list[str] = []
    sanitized = _sanitize_value(
        value,
        path=root_path,
        key_hint=None,
        secrets=secrets,
        discovered=discovered,
        excluded=excluded,
        active=set(),
    )
    if not isinstance(sanitized, dict):
        raise TypeError("sanitized collector configuration must be an object")
    return sanitized, _ordered_unique(excluded), tuple(discovered)


def _sanitize_value(
    value: Any,
    *,
    path: str,
    key_hint: str | None,
    secrets: list[str | bytes],
    discovered: list[str | bytes],
    excluded: list[str],
    active: set[int],
) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        excluded.append(path)
        return _OMIT
    if isinstance(value, str):
        if _contains_secret(value, secrets):
            excluded.append(path)
            return _REDACTED
        if key_hint is not None and _looks_like_url_key(key_hint):
            discovered_before = len(discovered)
            sanitized_url = _sanitize_url(value, discovered)
            for secret in discovered[discovered_before:]:
                if secret not in secrets:
                    secrets.append(secret)
            return sanitized_url
        return value
    if isinstance(value, bytes):
        if _contains_secret(value, secrets):
            excluded.append(path)
            return _REDACTED
        excluded.append(path)
        return _OMIT
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _sanitize_value(
            value.value,
            path=path,
            key_hint=key_hint,
            secrets=secrets,
            discovered=discovered,
            excluded=excluded,
            active=active,
        )
    if callable(value):
        excluded.append(path)
        return _OMIT

    identity = id(value)
    if identity in active:
        excluded.append(path)
        return _OMIT
    if isinstance(value, Mapping):
        active.add(identity)
        result: dict[str, Any] = {}
        try:
            for key, child in value.items():
                if not isinstance(key, str):
                    excluded.append(f"{path}.<non-string-key>")
                    continue
                child_path = f"{path}.{key}"
                if _is_credential_key(key):
                    _discover_secret_values(child, secrets, discovered)
                    excluded.append(child_path)
                    continue
                child_value = _sanitize_value(
                    child,
                    path=child_path,
                    key_hint=key,
                    secrets=secrets,
                    discovered=discovered,
                    excluded=excluded,
                    active=active,
                )
                if child_value is not _OMIT:
                    result[key] = child_value
        finally:
            active.remove(identity)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        active.add(identity)
        items: list[Any] = []
        try:
            for index, child in enumerate(value):
                child_value = _sanitize_value(
                    child,
                    path=f"{path}[{index}]",
                    key_hint=key_hint,
                    secrets=secrets,
                    discovered=discovered,
                    excluded=excluded,
                    active=active,
                )
                if child_value is not _OMIT:
                    items.append(child_value)
        finally:
            active.remove(identity)
        return items
    excluded.append(path)
    return _OMIT


def _discover_secret_values(
    value: Any,
    secrets: list[str | bytes],
    discovered: list[str | bytes],
) -> None:
    if isinstance(value, (str, bytes)):
        if value and not is_placeholder_credential(value) and value not in secrets:
            secrets.append(value)
            discovered.append(value)
        return
    if isinstance(value, Mapping):
        for child in value.values():
            _discover_secret_values(child, secrets, discovered)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _discover_secret_values(child, secrets, discovered)


def _sanitize_url(value: str, discovered: list[str | bytes]) -> str:
    try:
        split = urlsplit(value)
    except ValueError:
        return value
    if not split.scheme or not split.netloc:
        return value
    if split.password:
        discovered.append(split.password)
    safe_host = split.hostname or ""
    try:
        port = split.port
    except ValueError:
        return value
    if port is not None:
        safe_host = f"{safe_host}:{port}"
    for key, item in parse_qsl(split.query, keep_blank_values=True):
        if _normalized_key(key) in _SENSITIVE_QUERY_KEYS and item:
            discovered.append(item)
    return urlunsplit((split.scheme, safe_host, "", "", ""))


def _agent_metadata(
    agent: Any,
    configured_secrets: Iterable[str | bytes] = (),
) -> dict[str, Any]:
    if isinstance(agent, Mapping):
        value, _, _ = sanitize_collector_config(
            agent,
            configured_secrets=configured_secrets,
            root_path="task.agent",
        )
        return value
    metadata: dict[str, Any] = {
        "adapter": type(agent).__name__,
        "adapter_module": type(agent).__module__,
        "configuration": {},
    }
    model = getattr(agent, "model_name", None)
    if isinstance(model, str):
        metadata["model"] = _scrub_text(model, configured_secrets)
    return metadata


def _environment_metadata(
    environment: Any,
    configured_secrets: Iterable[str | bytes] = (),
) -> dict[str, Any]:
    if isinstance(environment, Mapping):
        raw = dict(environment)
        base_url = raw.pop("base_url", raw.pop("url", None))
        value, _, _ = sanitize_collector_config(
            raw,
            configured_secrets=configured_secrets,
            root_path="task.environment",
        )
    else:
        base_url = getattr(environment, "base_url", None)
        value = {
            "client": type(environment).__name__,
            "client_module": type(environment).__module__,
        }
        device = getattr(environment, "device", getattr(environment, "device_id", None))
        if isinstance(device, str):
            value["device_id"] = _scrub_text(device, configured_secrets)
    if isinstance(base_url, str):
        origin = _sanitize_url(base_url, [])
        origin = _scrub_text(origin, configured_secrets)
        value["backend_id"] = "backend-" + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
    else:
        value.setdefault("backend_id", "unavailable")
    return value


def _find_repository_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate.resolve(strict=False)
    raise RuntimeError("could not locate the Git repository root")


def detect_repository_dirty(
    repository_root: str | Path | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> bool | None:
    """Return the read-only Git worktree state, or ``None`` when unavailable."""

    try:
        root = (
            Path(repository_root).expanduser().resolve(strict=False)
            if repository_root is not None
            else _find_repository_root(Path(__file__))
        )
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
            ],
            cwd=root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout)


def _read_mobileworld_upstream(repository_root: Path) -> dict[str, str]:
    candidates = (
        repository_root / "MobileWorld" / "UPSTREAM.md",
        repository_root / "UPSTREAM.md",
    )
    provenance_path = next((path for path in candidates if path.is_file()), None)
    if provenance_path is None:
        raise RuntimeError("MobileWorld upstream provenance file is missing")
    document = provenance_path.read_text(encoding="utf-8")
    section_match = re.search(
        r"(?ms)^## MobileWorld\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        document,
    )
    if section_match is None:
        raise RuntimeError("MobileWorld upstream provenance section is missing")
    body = section_match.group("body")
    url_match = re.search(r"Upstream repository:\s*`([^`]+)`", body)
    commit_match = re.search(r"Imported commit:\s*`([0-9A-Fa-f]{40})`", body)
    if url_match is None or commit_match is None:
        raise RuntimeError("MobileWorld upstream URL or commit is missing")
    return {
        "repository_url": _validated_provenance_url(url_match.group(1)),
        "commit": _validated_commit(commit_match.group(1)),
        "provenance_file": provenance_path.relative_to(repository_root).as_posix(),
    }


def _validated_provenance_url(value: str) -> str:
    split = urlsplit(value)
    if (
        split.scheme != "https"
        or split.hostname != "github.com"
        or split.username is not None
        or split.password is not None
        or split.query
        or split.fragment
    ):
        raise ValueError("MobileWorld upstream URL must be a plain HTTPS GitHub URL")
    return value


def _validated_commit(value: str | None) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value.lower()) is None:
        raise ValueError("MobileWorld upstream commit must be 40 hexadecimal characters")
    return value.lower()


def _read_git_commit(repository_root: Path) -> str:
    dot_git = repository_root / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        marker = dot_git.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir:"):
            raise RuntimeError("unsupported .git indirection file")
        git_dir = (repository_root / marker.partition(":")[2].strip()).resolve()
    else:
        raise RuntimeError("repository root does not contain .git metadata")

    head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    if not head.startswith("ref:"):
        if _COMMIT_RE.fullmatch(head.lower()) is None:
            raise RuntimeError("Git HEAD does not contain a valid commit")
        return head.lower()
    reference = head.partition(":")[2].strip()
    if not reference.startswith("refs/") or ".." in Path(reference).parts:
        raise RuntimeError("Git HEAD contains an invalid reference")

    search_roots = [git_dir]
    common_dir_path = git_dir / "commondir"
    if common_dir_path.is_file():
        common_value = common_dir_path.read_text(encoding="utf-8").strip()
        search_roots.append((git_dir / common_value).resolve())
    for root in search_roots:
        loose = root / reference
        if loose.is_file():
            commit = loose.read_text(encoding="ascii").strip().lower()
            if _COMMIT_RE.fullmatch(commit):
                return commit
        packed = root / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="ascii").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                commit, _, name = line.partition(" ")
                if name == reference and _COMMIT_RE.fullmatch(commit.lower()):
                    return commit.lower()
    raise RuntimeError(f"could not resolve Git reference {reference!r}")


def _file_summary(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return {"sha256": digest.hexdigest(), "byte_count": byte_count}


def _close_best_effort(recorder: RunRecorder) -> None:
    try:
        recorder.close()
    except Exception:
        pass


def _blob_summary(root: Path) -> dict[str, int]:
    count = 0
    byte_count = 0
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                count += 1
                byte_count += path.stat().st_size
    return {"count": count, "byte_count": byte_count}


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _normalize_configured_secrets(
    values: Iterable[str | bytes],
) -> tuple[str | bytes, ...]:
    normalized: list[str | bytes] = []
    for value in values:
        if not isinstance(value, (str, bytes)):
            raise TypeError("configured secrets must be strings or bytes")
        if not value or is_placeholder_credential(value) or value in normalized:
            continue
        normalized.append(value)
    return tuple(normalized)


def _contains_secret(value: str | bytes, secrets: Iterable[str | bytes]) -> bool:
    for secret in secrets:
        if isinstance(value, str):
            if isinstance(secret, bytes):
                try:
                    secret_text = secret.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            else:
                secret_text = secret
            if secret_text in value:
                return True
        if isinstance(value, bytes):
            encoded = secret.encode("utf-8") if isinstance(secret, str) else secret
            if encoded in value:
                return True
    return False


def _scrub_text(value: str, secrets: Iterable[str | bytes]) -> str:
    result = value
    for secret in secrets:
        if isinstance(secret, bytes):
            try:
                secret_text = secret.decode("utf-8")
            except UnicodeDecodeError:
                continue
        else:
            secret_text = secret
        result = result.replace(secret_text, _REDACTED)
    return result


def _scrub_optional_text(
    value: str | None,
    secrets: Iterable[str | bytes],
) -> str | None:
    return _scrub_text(value, secrets) if isinstance(value, str) else None


def _credential_free_url(value: str, secrets: Iterable[str | bytes]) -> str:
    try:
        split = urlsplit(value)
        if not split.scheme or not split.hostname:
            return _scrub_text(value, secrets)
        hostname = split.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if split.port is not None:
            netloc = f"{netloc}:{split.port}"
        return _scrub_text(
            urlunsplit((split.scheme, netloc, split.path, "", "")),
            secrets,
        )
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def _provider_sdk_configuration(
    agent_type: str | None,
    openai_version: str | None,
) -> dict[str, Any]:
    try:
        from openai import DEFAULT_MAX_RETRIES

        default_max_retries: int | None = DEFAULT_MAX_RETRIES
    except (ImportError, AttributeError):
        default_max_retries = None

    configuration: dict[str, Any] = {
        "application_visible_sdk": "openai.chat.completions.create",
        "openai_version": openai_version,
        "transparent_http_attempts_observable": False,
        "actor": {
            "timeout_seconds": 120.0,
            "max_retries": default_max_retries,
            "max_retries_source": "openai_sdk_default",
        },
    }
    if agent_type == "planner_executor":
        configuration["uiins_grounder"] = {
            "timeout_seconds": 60.0,
            "max_retries": 3,
            "max_retries_source": "explicit_constructor_argument",
        }
    return configuration


def _is_credential_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(_CREDENTIAL_SUFFIXES)


def _looks_like_url_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized == "url" or normalized.endswith("_url")


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AuditLifecycle",
    "DEGRADED_AUDIT_LIFECYCLE",
    "DegradedAuditLifecycle",
    "NULL_AUDIT_LIFECYCLE",
    "NullAuditLifecycle",
    "TaskAuditBinding",
    "bootstrap_audit_run",
    "detect_repository_dirty",
    "sanitize_collector_config",
]
