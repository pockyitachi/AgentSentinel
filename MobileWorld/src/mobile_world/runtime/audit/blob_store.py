"""Content-addressed, immutable blob storage for raw audit artifacts."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict


class BlobRef(TypedDict):
    """The exact blob-reference shape required by the v1 event contract."""

    algorithm: Literal["sha256"]
    digest: str
    byte_length: int
    media_type: str
    relative_path: str


class BlobStoreError(RuntimeError):
    """Base class for blob-store failures."""


class BlobIntegrityError(BlobStoreError):
    """Raised when stored bytes do not match their content address."""


class BlobStore:
    """Store immutable blobs beneath ``<run_root>/blobs/sha256``.

    Installation uses an atomic hard link from a fully flushed temporary file.
    Unlike ``os.replace``, this can never overwrite an existing digest path.
    Concurrent writers of identical bytes converge on the same verified file.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def put_bytes(self, data: bytes | bytearray | memoryview, media_type: str) -> BlobRef:
        """Persist exact *data* bytes and return their contract ``BlobRef``."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        if not isinstance(media_type, str) or not media_type.strip():
            raise ValueError("media_type must be a non-empty string")

        exact_bytes = bytes(data)
        digest = hashlib.sha256(exact_bytes).hexdigest()
        relative_path = PurePosixPath("blobs", "sha256", digest[:2], digest)
        final_path = self.root.joinpath(*relative_path.parts)
        self._ensure_private_directory(final_path.parent)

        if final_path.exists():
            self._verify_path(final_path, digest, len(exact_bytes))
            return self._ref(digest, len(exact_bytes), media_type, relative_path)

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=final_path.parent)
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                temporary_file.write(exact_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            self._verify_path(temporary_path, digest, len(exact_bytes))
            try:
                # Same-directory hard linking atomically publishes complete bytes
                # and fails with EEXIST instead of replacing immutable evidence.
                os.link(temporary_path, final_path)
                os.chmod(final_path, 0o600)
                self._fsync_directory(final_path.parent)
            except FileExistsError:
                self._verify_path(final_path, digest, len(exact_bytes))
            except OSError as error:
                if error.errno == errno.EEXIST:
                    self._verify_path(final_path, digest, len(exact_bytes))
                else:
                    raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

        self._verify_path(final_path, digest, len(exact_bytes))
        return self._ref(digest, len(exact_bytes), media_type, relative_path)

    def resolve(self, reference: BlobRef) -> Path:
        """Resolve a reference after validating its canonical digest path."""

        digest, byte_length, _ = self._validate_ref(reference)
        expected_relative = PurePosixPath("blobs", "sha256", digest[:2], digest)
        supplied_relative = PurePosixPath(reference["relative_path"])
        if supplied_relative != expected_relative or supplied_relative.is_absolute():
            raise BlobIntegrityError("blob relative_path does not match its digest")
        path = self.root.joinpath(*expected_relative.parts)
        self._verify_path(path, digest, byte_length)
        return path

    def verify(self, reference: BlobRef) -> bool:
        """Verify a reference's path, byte count, and SHA-256 digest."""

        self.resolve(reference)
        return True

    def read_bytes(self, reference: BlobRef) -> bytes:
        """Read a blob only after verifying the immutable reference."""

        path = self.resolve(reference)
        data = path.read_bytes()
        # Recheck after reading to catch a concurrent/manual corruption race.
        digest, byte_length, _ = self._validate_ref(reference)
        if len(data) != byte_length or hashlib.sha256(data).hexdigest() != digest:
            raise BlobIntegrityError(f"blob changed while being read: {path}")
        return data

    @staticmethod
    def _ref(
        digest: str, byte_length: int, media_type: str, relative_path: PurePosixPath
    ) -> BlobRef:
        return {
            "algorithm": "sha256",
            "digest": digest,
            "byte_length": byte_length,
            "media_type": media_type,
            "relative_path": relative_path.as_posix(),
        }

    @staticmethod
    def _validate_ref(reference: BlobRef) -> tuple[str, int, str]:
        if not isinstance(reference, dict):
            raise BlobIntegrityError("blob reference must be a mapping")
        required = {"algorithm", "digest", "byte_length", "media_type", "relative_path"}
        if set(reference) != required:
            raise BlobIntegrityError("blob reference has an invalid shape")
        if reference["algorithm"] != "sha256":
            raise BlobIntegrityError("unsupported blob digest algorithm")
        digest = reference["digest"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BlobIntegrityError("invalid SHA-256 digest")
        byte_length = reference["byte_length"]
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 0:
            raise BlobIntegrityError("invalid blob byte_length")
        media_type = reference["media_type"]
        if not isinstance(media_type, str) or not media_type.strip():
            raise BlobIntegrityError("invalid blob media_type")
        relative_path = reference["relative_path"]
        if not isinstance(relative_path, str) or not relative_path:
            raise BlobIntegrityError("invalid blob relative_path")
        return digest, byte_length, media_type

    @staticmethod
    def _verify_path(path: Path, expected_digest: str, expected_length: int) -> None:
        if path.is_symlink() or not path.is_file():
            raise BlobIntegrityError(f"blob is missing or is not a regular file: {path}")
        stat = path.stat()
        if stat.st_size != expected_length:
            raise BlobIntegrityError(
                f"blob byte length mismatch at {path}: {stat.st_size} != {expected_length}"
            )
        hasher = hashlib.sha256()
        with path.open("rb") as blob_file:
            for chunk in iter(lambda: blob_file.read(1024 * 1024), b""):
                hasher.update(chunk)
        actual_digest = hasher.hexdigest()
        if actual_digest != expected_digest:
            raise BlobIntegrityError(
                f"blob digest mismatch at {path}: {actual_digest} != {expected_digest}"
            )

    def _ensure_private_directory(self, directory: Path) -> None:
        # Build only paths beneath the caller-selected run root.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = directory
        while current != self.root.parent and self.root in (current, *current.parents):
            try:
                os.chmod(current, 0o700)
            except FileNotFoundError:
                pass
            if current == self.root:
                break
            current = current.parent

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["BlobIntegrityError", "BlobRef", "BlobStore", "BlobStoreError"]
