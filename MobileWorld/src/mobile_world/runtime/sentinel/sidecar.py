"""Hash-only receipt sinks for the R2.1 runtime seam."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from threading import Lock

from mobile_world.offline.causal_replay.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.contracts import SentinelReceipt


class MemorySentinelReceiptSink:
    """Thread-safe test/embedding sink retaining no request or exact-diff bytes."""

    def __init__(self) -> None:
        self._receipts: list[SentinelReceipt] = []
        self._lock = Lock()

    @property
    def receipts(self) -> tuple[SentinelReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def emit(self, receipt: SentinelReceipt) -> None:
        with self._lock:
            self._receipts.append(receipt)


class ExternalSentinelReceiptSink:
    """Write one canonical, owner-only receipt per logical call outside Git.

    The v1 sink deliberately has no API for request views or exact diff text.
    A later detail sink must remain repo-external and apply the credential
    exclusion/redaction policy before it is allowed to persist such bytes.
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
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def emit(self, receipt: SentinelReceipt) -> None:
        payload = canonical_json_bytes(receipt.to_dict())
        destination = f"{receipt.logical_call_id}.sentinel-receipt.v1.json"
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        with self._lock:
            try:
                directory_fd = os.open(self._root, directory_flags)
            except OSError as exc:
                raise OSError("Sentinel receipt root cannot be reopened safely") from exc
            try:
                root_info = os.fstat(directory_fd)
                if (
                    not stat.S_ISDIR(root_info.st_mode)
                    or (
                        root_info.st_dev,
                        root_info.st_ino,
                        root_info.st_uid,
                        root_info.st_gid,
                    )
                    != self._root_identity
                    or stat.S_IMODE(root_info.st_mode) & 0o077
                ):
                    raise OSError("Sentinel receipt root identity changed")
                self._publish_transactionally(
                    directory_fd=directory_fd,
                    destination=destination,
                    payload=payload,
                )
            finally:
                os.close(directory_fd)

    @staticmethod
    def _read_existing(*, directory_fd: int, name: str) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
            ):
                raise OSError("existing Sentinel receipt metadata is invalid")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65_536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    @classmethod
    def _publish_transactionally(
        cls,
        *,
        directory_fd: int,
        destination: str,
        payload: bytes,
    ) -> None:
        """Publish complete bytes atomically without replacing an existing receipt.

        A private temporary inode is fully written, synced, and validated before
        an atomic hard-link creates the final name.  Once that link succeeds the
        receipt is committed; later cleanup is best-effort so the caller never
        falls back to Original while a valid ACTIVE receipt is already visible.
        """

        temp_name = f".{destination}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        published = False
        try:
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short Sentinel receipt write")
                    view = view[written:]
                os.fsync(fd)
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()
                    or info.st_gid != os.getegid()
                    or info.st_size != len(payload)
                ):
                    raise OSError("Sentinel receipt metadata changed before publication")
            finally:
                os.close(fd)

            try:
                os.link(
                    temp_name,
                    destination,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = cls._read_existing(
                    directory_fd=directory_fd,
                    name=destination,
                )
                if existing != payload:
                    raise FileExistsError(
                        "logical-call receipt already exists with different bytes"
                    ) from None
                return
            published = True
        finally:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                if not published:
                    raise
            if published:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    # The complete final inode is already atomically visible.  An
                    # error here must not make the actor send Original against an
                    # ACTIVE receipt that was successfully published.
                    pass


__all__ = ["ExternalSentinelReceiptSink", "MemorySentinelReceiptSink"]
