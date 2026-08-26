"""Crash-aware append-only persistence for raw MobileWorld audit events."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

try:  # pragma: no cover - MobileWorld's supported server runtime is POSIX.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.schemas import (
    SCHEMA_VERSION,
    Producer,
    build_event,
    validate_collector_metadata_keys,
    validate_event_envelope,
)


class RecorderError(RuntimeError):
    """Base class for recorder lifecycle and persistence failures."""


class RecorderClosedError(RecorderError):
    """Raised when an append is attempted after close or finalization."""


class RecorderFinalizedError(RecorderClosedError):
    """Raised when code attempts to reopen or mutate a finalized run."""


class StreamCorruptionError(RecorderError):
    """Raised rather than appending after a malformed or partial JSONL tail."""


class _EventStream:
    """One append-only event file with cooperating thread/process locking."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        task_run_id: str | None,
        producer: Producer | Mapping[str, Any],
        sync: bool,
    ) -> None:
        _ensure_private_directory(path.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        self.path = path
        self._file: BinaryIO = os.fdopen(descriptor, "r+b", buffering=0)
        self._run_id = run_id
        self._task_run_id = task_run_id
        self._producer = producer
        self._sync = sync
        self._lock = threading.RLock()
        self._closed = False

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        caused_by_event_id: str | None,
    ) -> dict[str, Any]:
        """Build and durably append one complete JSONL record."""

        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        with self._lock:
            self._ensure_open()
            with _advisory_file_lock(self._file):
                seq = self._next_sequence_locked()
                event = build_event(
                    event_id=new_ulid(),
                    event_type=event_type,
                    run_id=self._run_id,
                    task_run_id=self._task_run_id,
                    seq=seq,
                    producer=self._producer,
                    payload=payload,
                    caused_by_event_id=caused_by_event_id,
                )
                # Keep validation next to persistence even if build_event's
                # implementation later changes to support an unchecked mode.
                validate_event_envelope(event)
                encoded = _canonical_json_line(event)
                self._file.seek(0, os.SEEK_END)
                _write_all(self._file.fileno(), encoded)
                if self._sync:
                    os.fsync(self._file.fileno())
                return event

    def flush(self) -> None:
        """Make all complete records visible and durable."""

        with self._lock:
            self._ensure_open()
            with _advisory_file_lock(self._file):
                os.fsync(self._file.fileno())

    def close(self) -> None:
        """Flush and close this writer; closing twice is harmless."""

        with self._lock:
            if self._closed:
                return
            with _advisory_file_lock(self._file):
                os.fsync(self._file.fileno())
            self._file.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RecorderClosedError(f"event stream is closed: {self.path}")

    def _next_sequence_locked(self) -> int:
        """Read the last durable record while holding the interprocess lock."""

        descriptor = self._file.fileno()
        size = os.fstat(descriptor).st_size
        if size == 0:
            return 1
        if os.pread(descriptor, 1, size - 1) != b"\n":
            raise StreamCorruptionError(
                f"refusing to append after an incomplete JSONL tail: {self.path}"
            )

        line = _read_last_complete_line(descriptor, size)
        if not line:
            raise StreamCorruptionError(f"empty JSONL record at end of {self.path}")
        try:
            previous = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamCorruptionError(
                f"last JSONL record is not valid UTF-8 JSON: {self.path}"
            ) from error
        if not isinstance(previous, dict):
            raise StreamCorruptionError(f"last JSONL record is not an object: {self.path}")

        expected_stream_id = self._task_run_id or self._run_id
        if previous.get("stream_id") != expected_stream_id:
            raise StreamCorruptionError(f"last record belongs to another stream: {self.path}")
        previous_seq = previous.get("seq")
        if isinstance(previous_seq, bool) or not isinstance(previous_seq, int) or previous_seq < 1:
            raise StreamCorruptionError(f"last record has an invalid seq: {self.path}")
        return previous_seq + 1


class TaskRecorder:
    """Append-only writer bound to one physical task attempt."""

    enabled = True

    def __init__(self, owner: RunRecorder, task_run_id: str, stream: _EventStream) -> None:
        self._owner = owner
        self.task_run_id = task_run_id
        self._stream = stream
        self._closed = False
        self._lock = threading.RLock()
        self._capture_complete = True
        self._missing_artifacts: list[str] = []
        self._collector_error_event_ids: list[str] = []

    @property
    def path(self) -> Path:
        """Return this task's authoritative JSONL path."""

        return self._stream.path

    @property
    def blob_store(self) -> BlobStore:
        """Return the run-scoped content-addressed blob store."""

        return self._owner.blob_store

    @property
    def collector_mode(self) -> CollectorMode:
        """Return the run's normalized collector failure policy."""

        return self._owner.collector_mode

    @property
    def capture_complete(self) -> bool:
        """Whether all hooks have so far reported complete evidence."""

        with self._lock:
            return self._capture_complete

    @property
    def missing_artifacts(self) -> tuple[str, ...]:
        """Return factual missing-artifact names in first-observed order."""

        with self._lock:
            return tuple(self._missing_artifacts)

    @property
    def collector_error_event_ids(self) -> tuple[str, ...]:
        """Return persisted collector-error IDs in event order."""

        with self._lock:
            return tuple(self._collector_error_event_ids)

    def mark_incomplete(self, *missing_artifacts: str) -> None:
        """Record in-memory incompleteness even if the error stream is unavailable."""

        with self._lock:
            self._capture_complete = False
            for artifact in missing_artifacts:
                if artifact and artifact not in self._missing_artifacts:
                    self._missing_artifacts.append(artifact)

    def append_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one task event and return the exact persisted envelope."""

        with self._lock:
            if self._closed:
                raise RecorderClosedError(f"task recorder is closed: {self.task_run_id}")
            event = self._owner._append_task_event(
                self._stream,
                event_type,
                payload,
                caused_by_event_id=caused_by_event_id,
            )
            if event_type == "collector_error":
                missing = payload.get("missing_artifacts")
                if isinstance(missing, list):
                    self.mark_incomplete(*(item for item in missing if isinstance(item, str)))
                else:
                    self.mark_incomplete("unspecified_collector_artifact")
                event_id = event.get("event_id")
                if isinstance(event_id, str) and event_id not in self._collector_error_event_ids:
                    self._collector_error_event_ids.append(event_id)
            return event

    def flush(self) -> None:
        """Durably flush this task stream."""

        with self._lock:
            if self._closed:
                raise RecorderClosedError(f"task recorder is closed: {self.task_run_id}")
            self._stream.flush()

    def close(self) -> None:
        """Close this task writer; closing twice is harmless."""

        with self._lock:
            if self._closed:
                return
            self._stream.close()
            self._closed = True

    def _flush_for_finalization(self) -> None:
        """Flush an open stream; a closed stream was already fsynced."""

        with self._lock:
            if not self._closed:
                self._stream.flush()

    def __enter__(self) -> TaskRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class RunRecorder:
    """Own one run directory and its independently ordered event streams.

    ``audit_root`` is the configured collection root.  Evidence is stored at
    ``<audit_root>/raw/runs/<run_id>`` exactly as specified by the v1 contract.
    A run ID is allocated when omitted, before any task/environment work.
    """

    enabled = True

    def __init__(
        self,
        audit_root: str | os.PathLike[str],
        *,
        producer: Producer | Mapping[str, Any],
        run_id: str | None = None,
        collector_mode: CollectorMode | str = (CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER),
        sync: bool = True,
    ) -> None:
        self.collector_mode = CollectorMode(collector_mode)
        self.run_id = run_id or new_ulid()
        _validate_stream_identity(self.run_id, None, producer)
        self.audit_root = Path(audit_root)
        self.run_root = self.audit_root / "raw" / "runs" / self.run_id
        self.manifest_start_path = self.run_root / "manifest.start.json"
        self.manifest_final_path = self.run_root / "manifest.final.json"
        if self.manifest_final_path.exists():
            raise RecorderFinalizedError(f"run is already finalized: {self.run_id}")

        _ensure_private_directory(self.run_root)
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_descriptor = os.open(self.run_root / ".writer.lock", lock_flags, 0o600)
        os.fchmod(lock_descriptor, 0o600)
        self._writer_lock: BinaryIO = os.fdopen(lock_descriptor, "r+b", buffering=0)
        self.blob_store = BlobStore(self.run_root)
        self._producer = producer if isinstance(producer, Producer) else dict(producer)
        self._sync = sync
        self._condition = threading.Condition(threading.RLock())
        self._active_appends = 0
        self._finalizing = False
        self._finalized = False
        self._closed = False
        self._tasks: dict[str, TaskRecorder] = {}
        self._run_stream = _EventStream(
            self.run_root / "run.events.jsonl",
            run_id=self.run_id,
            task_run_id=None,
            producer=self._producer,
            sync=sync,
        )

    def write_manifest_start(self, manifest: Mapping[str, Any]) -> Path:
        """Create ``manifest.start.json`` once without replacement."""

        prepared = self._prepare_manifest(manifest)
        with self._condition:
            self._ensure_mutable_locked()
            _write_exclusive_json(self.manifest_start_path, prepared)
        return self.manifest_start_path

    def append_run_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one event to the separately ordered run lifecycle stream."""

        with _advisory_file_lock(self._writer_lock, shared=True):
            self._begin_append()
            try:
                return self._run_stream.append(
                    event_type,
                    payload,
                    caused_by_event_id=caused_by_event_id,
                )
            finally:
                self._end_append()

    def open_task(self, task_run_id: str | None = None) -> TaskRecorder:
        """Return the single writer for one task-attempt stream."""

        effective_id = task_run_id or new_ulid()
        _validate_stream_identity(self.run_id, effective_id, self._producer)
        with _advisory_file_lock(self._writer_lock, shared=True):
            with self._condition:
                self._ensure_mutable_locked()
                existing = self._tasks.get(effective_id)
                if existing is not None:
                    return existing
                stream = _EventStream(
                    self.run_root / "tasks" / effective_id / "events.jsonl",
                    run_id=self.run_id,
                    task_run_id=effective_id,
                    producer=self._producer,
                    sync=self._sync,
                )
                recorder = TaskRecorder(self, effective_id, stream)
                self._tasks[effective_id] = recorder
                return recorder

    def write_manifest_final(self, manifest: Mapping[str, Any]) -> Path:
        """Quiesce writers and create an immutable final manifest."""

        prepared = self._prepare_manifest(manifest)
        try:
            with _advisory_file_lock(self._writer_lock):
                with self._condition:
                    self._ensure_mutable_locked()
                    self._finalizing = True
                    while self._active_appends:
                        self._condition.wait()

                self._run_stream.flush()
                for task in self._tasks.values():
                    task._flush_for_finalization()
                _fsync_existing_event_streams(self.run_root)
                _write_exclusive_json(self.manifest_final_path, prepared)
        except BaseException:
            with self._condition:
                self._finalizing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._finalized = True
            self._finalizing = False
            self._condition.notify_all()
        return self.manifest_final_path

    def close(self) -> None:
        """Close all file handles without inventing a final manifest."""

        with self._condition:
            if self._closed:
                return
            self._finalizing = True
            while self._active_appends:
                self._condition.wait()

        for task in self._tasks.values():
            task.close()
        self._run_stream.close()
        self._writer_lock.close()
        with self._condition:
            self._closed = True
            self._finalizing = False
            self._condition.notify_all()

    def _append_task_event(
        self,
        stream: _EventStream,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        caused_by_event_id: str | None,
    ) -> dict[str, Any]:
        with _advisory_file_lock(self._writer_lock, shared=True):
            self._begin_append()
            try:
                return stream.append(
                    event_type,
                    payload,
                    caused_by_event_id=caused_by_event_id,
                )
            finally:
                self._end_append()

    def _begin_append(self) -> None:
        with self._condition:
            self._ensure_mutable_locked()
            if not self.manifest_start_path.is_file():
                raise RecorderError("manifest.start.json must exist before events are appended")
            self._active_appends += 1

    def _end_append(self) -> None:
        with self._condition:
            self._active_appends -= 1
            if self._active_appends == 0:
                self._condition.notify_all()

    def _ensure_mutable_locked(self) -> None:
        if self._finalized or self.manifest_final_path.exists():
            raise RecorderFinalizedError(f"run is finalized: {self.run_id}")
        if self._closed:
            raise RecorderClosedError(f"run recorder is closed: {self.run_id}")
        if self._finalizing:
            raise RecorderClosedError(f"run recorder is finalizing: {self.run_id}")

    def _prepare_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(manifest, Mapping):
            raise TypeError("manifest must be a mapping")
        prepared = dict(manifest)
        if prepared.get("run_id", self.run_id) != self.run_id:
            raise ValueError("manifest run_id does not match recorder run_id")
        if prepared.get("raw_schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ValueError(f"manifest raw_schema_version must be {SCHEMA_VERSION!r}")
        prepared.setdefault("raw_schema_version", SCHEMA_VERSION)
        prepared.setdefault("run_id", self.run_id)
        validate_collector_metadata_keys(prepared)
        # Serialize before opening the exclusive destination, so a type error
        # cannot leave behind a zero-length manifest that looks committed.
        _canonical_json_line(prepared)
        return prepared

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _validate_stream_identity(
    run_id: str,
    task_run_id: str | None,
    producer: Producer | Mapping[str, Any],
) -> None:
    """Delegate ID and producer validation to the shared schema module."""

    build_event(
        event_id=new_ulid(),
        event_type="task_started" if task_run_id is not None else "run_started",
        run_id=run_id,
        task_run_id=task_run_id,
        seq=1,
        producer=producer,
        payload={},
    )


def _canonical_json_line(value: Mapping[str, Any]) -> bytes:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise RecorderError("audit value is not losslessly JSON serializable") from error
    return serialized.encode("utf-8") + b"\n"


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical_json_line(value)
    _ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    except BaseException:
        # The destination did not exist before this call.  Removing an
        # incomplete creation is safe and avoids a false committed marker.
        os.close(descriptor)
        descriptor = -1
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("zero-byte write while persisting audit evidence")
        view = view[written:]


def _read_last_complete_line(descriptor: int, size: int) -> bytes:
    cursor = size - 1  # Exclude the required final newline.
    chunks: list[bytes] = []
    while cursor > 0:
        start = max(0, cursor - 64 * 1024)
        chunk = os.pread(descriptor, cursor - start, start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            chunks.insert(0, chunk[newline + 1 :])
            break
        chunks.insert(0, chunk)
        cursor = start
    return b"".join(chunks)


@contextmanager
def _advisory_file_lock(file: BinaryIO, *, shared: bool = False) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    fcntl.flock(file.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


def _fsync_existing_event_streams(run_root: Path) -> None:
    paths = [run_root / "run.events.jsonl"]
    tasks_root = run_root / "tasks"
    if tasks_root.is_dir():
        for task_directory in tasks_root.iterdir():
            if task_directory.is_symlink() or not task_directory.is_dir():
                continue
            paths.append(task_directory / "events.jsonl")
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "RecorderClosedError",
    "RecorderError",
    "RecorderFinalizedError",
    "RunRecorder",
    "StreamCorruptionError",
    "TaskRecorder",
]
