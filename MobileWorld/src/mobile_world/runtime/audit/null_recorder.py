"""True no-op recorder implementations for the default-disabled path."""

from __future__ import annotations

from typing import Any

from mobile_world.runtime.audit.config import CollectorMode


class NullTaskRecorder:
    """Task-recorder surface that performs no serialization or I/O."""

    __slots__ = ()
    enabled = False
    blob_store = None
    collector_mode = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    task_run_id = None
    path = None
    capture_complete = True
    missing_artifacts: tuple[str, ...] = ()
    collector_error_event_ids: tuple[str, ...] = ()

    def mark_incomplete(self, *missing_artifacts: str) -> None:
        return None

    def append_event(
        self,
        event_type: str,
        payload: Any,
        caused_by_event_id: str | None = None,
    ) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> NullTaskRecorder:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


NULL_TASK_RECORDER = NullTaskRecorder()


class NullRecorder:
    """Run-recorder surface used whenever collection is disabled.

    Every method intentionally ignores its arguments.  In particular, none of
    them iterates payloads, opens paths, creates manifests, hashes data, or
    imports a serializer.  Call sites must also avoid eagerly serializing an
    argument before invoking this no-op surface.
    """

    __slots__ = ()
    enabled = False
    run_id = None
    blob_store = None
    collector_mode = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER

    def write_manifest_start(self, manifest: Any) -> None:
        return None

    def append_run_event(
        self,
        event_type: str,
        payload: Any,
        caused_by_event_id: str | None = None,
    ) -> None:
        return None

    def open_task(self, task_run_id: str | None = None) -> NullTaskRecorder:
        return NULL_TASK_RECORDER

    def write_manifest_final(self, manifest: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> NullRecorder:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


NULL_RECORDER = NullRecorder()
NullRunRecorder = NullRecorder
