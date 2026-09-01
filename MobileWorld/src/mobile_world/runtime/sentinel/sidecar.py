"""Hash-only receipt sinks for the R2.1 runtime seam."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from threading import Lock

from mobile_world.offline.causal_replay.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.contracts import SentinelReceipt

_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ADMISSION_PROBE = b"mobileworld-sentinel-receipt-admission-v1\n"


def _validate_logical_call_id(logical_call_id: str) -> None:
    if not isinstance(logical_call_id, str) or _RUNTIME_ID.fullmatch(logical_call_id) is None:
        raise ValueError("logical_call_id must be a bounded path-safe runtime ID")


class _MemoryReceiptTransaction:
    def __init__(self, sink: MemorySentinelReceiptSink, logical_call_id: str) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._finished = False
        self._lock = Lock()

    def commit(self, receipt: SentinelReceipt) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("Sentinel receipt transaction is already finished")
            if receipt.logical_call_id != self._logical_call_id:
                raise ValueError("receipt logical_call_id differs from its transaction")
            self._sink._commit(self._logical_call_id, receipt)
            self._finished = True

    def abort(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._sink._abort(self._logical_call_id)
            self._finished = True


class MemorySentinelReceiptSink:
    """Thread-safe test/embedding sink retaining no request or exact-diff bytes."""

    def __init__(self) -> None:
        self._receipts: list[SentinelReceipt] = []
        self._active_ids: set[str] = set()
        self._committed_ids: set[str] = set()
        self._lock = Lock()

    @property
    def receipts(self) -> tuple[SentinelReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def begin(self, logical_call_id: str) -> _MemoryReceiptTransaction:
        _validate_logical_call_id(logical_call_id)
        with self._lock:
            if logical_call_id in self._active_ids or logical_call_id in self._committed_ids:
                raise FileExistsError("logical-call receipt transaction already exists")
            self._active_ids.add(logical_call_id)
        return _MemoryReceiptTransaction(self, logical_call_id)

    def _commit(self, logical_call_id: str, receipt: SentinelReceipt) -> None:
        with self._lock:
            if logical_call_id not in self._active_ids:
                raise RuntimeError("Sentinel receipt transaction is not active")
            self._active_ids.remove(logical_call_id)
            self._committed_ids.add(logical_call_id)
            self._receipts.append(receipt)

    def _abort(self, logical_call_id: str) -> None:
        with self._lock:
            self._active_ids.discard(logical_call_id)


class _ExternalReceiptTransaction:
    def __init__(
        self,
        *,
        sink: ExternalSentinelReceiptSink,
        logical_call_id: str,
        directory_fd: int,
        file_fd: int,
        destination: str,
    ) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._destination = destination
        self._finished = False
        self._lock = Lock()

    def commit(self, receipt: SentinelReceipt) -> None:
        payload = canonical_json_bytes(receipt.to_dict())
        with self._lock:
            if self._finished:
                raise RuntimeError("Sentinel receipt transaction is already finished")
            if receipt.logical_call_id != self._logical_call_id:
                self._finish(published=False)
                raise ValueError("receipt logical_call_id differs from its transaction")
            published = False
            try:
                os.ftruncate(self._file_fd, 0)
                os.lseek(self._file_fd, 0, os.SEEK_SET)
                self._write_all(self._file_fd, payload)
                os.fsync(self._file_fd)
                info = os.fstat(self._file_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 0
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()
                    or info.st_gid != os.getegid()
                    or info.st_size != len(payload)
                ):
                    raise OSError("Sentinel receipt metadata changed before publication")
                self._sink._validate_open_root(self._directory_fd)
                os.link(
                    f"/proc/self/fd/{self._file_fd}",
                    self._destination,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=True,
                )
                published = True
            except Exception:
                self._finish(published=published)
                raise
            self._finish(published=True)

    def abort(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finish(published=False)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short Sentinel receipt write")
            view = view[written:]

    def _finish(self, *, published: bool) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            os.close(self._file_fd)
        except OSError:
            pass
        if published:
            try:
                os.fsync(self._directory_fd)
            except OSError:
                pass
        try:
            os.close(self._directory_fd)
        except OSError:
            pass
        self._sink._release(self._logical_call_id)


class ExternalSentinelReceiptSink:
    """Admit and publish one owner-only receipt per logical call outside Git.

    ``begin`` verifies the fixed root and reserves a private, synced inode
    before semantic work. ``commit`` replaces its non-secret admission probe
    with the canonical receipt and atomically creates the final no-replace name.
    The v1 sink has no API for request views or exact diff text.
    """

    def __init__(self, root: Path, *, repository_root: Path | None = None) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("Sentinel receipt root must be an absolute path")
        repo = (
            Path(__file__).resolve().parents[5]
            if repository_root is None
            else repository_root.resolve()
        )
        resolved_parent = root.parent.resolve(strict=True)
        resolved = resolved_parent / root.name
        if resolved == repo or resolved.is_relative_to(repo):
            raise ValueError("Sentinel sidecars must remain outside the Git repository")
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Sentinel receipt root must be a real directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("Sentinel receipt root must be owner-only")
        self._root = resolved
        self._root_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        self._active_ids: set[str] = set()
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def begin(self, logical_call_id: str) -> _ExternalReceiptTransaction:
        _validate_logical_call_id(logical_call_id)
        destination = f"{logical_call_id}.sentinel-receipt.v1.json"
        with self._lock:
            if logical_call_id in self._active_ids:
                raise FileExistsError("logical-call receipt transaction already exists")
            self._active_ids.add(logical_call_id)

        directory_fd = -1
        file_fd = -1
        try:
            directory_fd = self._open_root()
            try:
                os.stat(destination, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("logical-call receipt already exists")
            if not hasattr(os, "O_TMPFILE"):
                raise OSError("anonymous receipt transactions are unavailable")
            file_fd = os.open(
                ".",
                os.O_RDWR | os.O_TMPFILE,
                0o600,
                dir_fd=directory_fd,
            )
            _ExternalReceiptTransaction._write_all(file_fd, _ADMISSION_PROBE)
            os.fsync(file_fd)
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_size != len(_ADMISSION_PROBE)
            ):
                raise OSError("Sentinel receipt admission metadata is invalid")
            link_source_info = os.stat(
                f"/proc/self/fd/{file_fd}",
                follow_symlinks=True,
            )
            if (link_source_info.st_dev, link_source_info.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise OSError("Sentinel receipt fd link source is unavailable")
            return _ExternalReceiptTransaction(
                sink=self,
                logical_call_id=logical_call_id,
                directory_fd=directory_fd,
                file_fd=file_fd,
                destination=destination,
            )
        except Exception:
            if file_fd >= 0:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
            if directory_fd >= 0:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
            self._release(logical_call_id)
            raise

    def _open_root(self) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(self._root, flags)
        except OSError as exc:
            raise OSError("Sentinel receipt root cannot be reopened safely") from exc
        try:
            info = os.fstat(directory_fd)
        except OSError:
            os.close(directory_fd)
            raise
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != self._root_identity
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(directory_fd)
            raise OSError("Sentinel receipt root identity changed")
        return directory_fd

    def _validate_open_root(self, directory_fd: int) -> None:
        pinned = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(pinned.st_mode)
            or (pinned.st_dev, pinned.st_ino, pinned.st_uid, pinned.st_gid) != self._root_identity
            or stat.S_IMODE(pinned.st_mode) & 0o077
        ):
            raise OSError("pinned Sentinel receipt root identity changed")
        reopened_fd = self._open_root()
        try:
            reopened = os.fstat(reopened_fd)
            if (reopened.st_dev, reopened.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("configured Sentinel receipt root was replaced")
        finally:
            os.close(reopened_fd)

    def _release(self, logical_call_id: str) -> None:
        with self._lock:
            self._active_ids.discard(logical_call_id)


__all__ = ["ExternalSentinelReceiptSink", "MemorySentinelReceiptSink"]
